# Copyright (c) 2026, Steve Morin
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""Repository discovery, status, and parallel fast-forward sync service."""

import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from repo_ffwd.core.models import (
    FAILURE_OUTCOMES,
    DiscoveredRepo,
    RepoListResult,
    RepoStatusItem,
    RepoStatusReport,
    RepoSyncItem,
    StatusFilter,
    StatusSyncState,
    SyncOutcome,
    SyncReport,
    matches_status_filter,
)
from repo_ffwd.core.ports import GitPort
from repo_ffwd.core.services.discovery import discover_repositories
from repo_ffwd.core.settings import RootConfig


def _short_error(stderr: str, fallback: str = "git error") -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[0] if lines else fallback


def _choose_remote(git: GitPort, repo: Path, preferred: str) -> str | None:
    remotes = git.list_remotes(repo)
    if not remotes:
        return None
    if preferred in remotes:
        return preferred
    if len(remotes) == 1:
        return remotes[0]
    return None


class RepoSyncService:
    """Application service for listing, inspecting, and syncing git repositories."""

    def __init__(self, git: GitPort) -> None:
        self._git = git

    def list_repos(
        self,
        roots: list[RootConfig],
        *,
        default_depth: int = 2,
        global_excludes: list[str] | None = None,
        include_worktrees: bool = False,
        tags: list[str] | None = None,
        preferred_remote: str = "origin",
    ) -> RepoListResult:
        found = discover_repositories(
            roots,
            git=self._git,
            default_depth=default_depth,
            global_excludes=global_excludes,
            include_worktrees=include_worktrees,
            tags=tags,
            preferred_remote=preferred_remote,
        )
        return RepoListResult(
            repos=found.repos,
            skipped_worktrees=found.skipped_worktrees,
            roots_scanned=found.roots_scanned,
        )

    def status_repos(
        self,
        roots: list[RootConfig],
        *,
        default_depth: int = 2,
        global_excludes: list[str] | None = None,
        include_worktrees: bool = False,
        tags: list[str] | None = None,
        preferred_remote: str = "origin",
        fetch: bool = False,
        concurrency: int = 6,
        timeout_seconds: int = 60,
        filters: Sequence[StatusFilter] | None = None,
        detailed: bool = False,
        cancel_event: Event | None = None,
    ) -> RepoStatusReport:
        applied_filters = list(filters) if filters else [StatusFilter.ALL]
        found = discover_repositories(
            roots,
            git=self._git,
            default_depth=default_depth,
            global_excludes=global_excludes,
            include_worktrees=include_worktrees,
            tags=tags,
            preferred_remote=preferred_remote,
        )
        if not found.repos:
            return RepoStatusReport(
                items=[],
                fetched=fetch,
                skipped_worktrees=len(found.skipped_worktrees),
                detailed=detailed,
                filters=applied_filters,
            )

        ordered: dict[int, RepoStatusItem] = {}
        workers = max(1, min(concurrency, len(found.repos)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures: dict[Future[RepoStatusItem], int] = {}
            for idx, repo in enumerate(found.repos):
                if cancel_event and cancel_event.is_set():
                    break
                futures[
                    pool.submit(
                        self._inspect_one,
                        repo,
                        preferred_remote=preferred_remote,
                        fetch=fetch,
                        timeout_seconds=timeout_seconds,
                    )
                ] = idx
            for fut in as_completed(futures):
                ordered[futures[fut]] = fut.result()

        items = [ordered[i] for i in sorted(ordered)]
        if StatusFilter.ALL not in applied_filters:
            items = [
                item
                for item in items
                if any(matches_status_filter(item, f) for f in applied_filters)
            ]

        return RepoStatusReport(
            items=items,
            fetched=fetch,
            skipped_worktrees=len(found.skipped_worktrees),
            detailed=detailed,
            filters=applied_filters,
        )

    def sync_repos(
        self,
        roots: list[RootConfig],
        *,
        default_depth: int = 2,
        global_excludes: list[str] | None = None,
        include_worktrees: bool = False,
        tags: list[str] | None = None,
        preferred_remote: str = "origin",
        concurrency: int = 6,
        timeout_seconds: int = 60,
        retries: int = 1,
        prune: bool = True,
        autostash: bool = False,
        dry_run: bool = False,
        only_changed: bool = False,
        fail_fast: bool = False,
        cancel_event: Event | None = None,
        on_progress: Callable[[RepoSyncItem], None] | None = None,
    ) -> SyncReport:
        found = discover_repositories(
            roots,
            git=self._git,
            default_depth=default_depth,
            global_excludes=global_excludes,
            include_worktrees=include_worktrees,
            tags=tags,
            preferred_remote=preferred_remote,
        )
        if not found.repos:
            return SyncReport(
                items=[],
                skipped_worktrees=len(found.skipped_worktrees),
                dry_run=dry_run,
                only_changed=only_changed,
            )

        stop_event = cancel_event or Event()
        ordered: dict[int, RepoSyncItem] = {}
        workers = max(1, min(concurrency, len(found.repos)))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures: dict[Future[RepoSyncItem], int] = {}
            for idx, repo in enumerate(found.repos):
                if stop_event.is_set():
                    break
                fut = pool.submit(
                    self._sync_one,
                    repo,
                    preferred_remote=preferred_remote,
                    timeout_seconds=timeout_seconds,
                    retries=retries,
                    prune=prune,
                    autostash=autostash,
                    dry_run=dry_run,
                    stop_event=stop_event,
                )
                futures[fut] = idx

            for fut in as_completed(futures):
                idx = futures[fut]
                item = fut.result()
                ordered[idx] = item
                if on_progress is not None:
                    on_progress(item)
                if fail_fast and item.outcome in FAILURE_OUTCOMES:
                    stop_event.set()
                    for pending in futures:
                        if not pending.done():
                            pending.cancel()

        return SyncReport(
            items=[ordered[i] for i in sorted(ordered)],
            skipped_worktrees=len(found.skipped_worktrees),
            dry_run=dry_run,
            only_changed=only_changed,
            cancelled=stop_event.is_set(),
        )

    def _inspect_one(
        self,
        repo: DiscoveredRepo,
        *,
        preferred_remote: str,
        fetch: bool,
        timeout_seconds: int,
    ) -> RepoStatusItem:
        path = Path(repo.path)
        branch = self._git.current_branch(path) or "(detached)"
        tracked, untracked = self._git.status_counts(path)
        remotes = self._git.list_remotes(path)
        remote: str | None = None
        if preferred_remote in remotes:
            remote = preferred_remote
        elif len(remotes) == 1:
            remote = remotes[0]

        detail = ""
        fetch_failed = False
        if remote and fetch:
            res = self._git.fetch(
                path,
                remote,
                prune=False,
                timeout_seconds=timeout_seconds,
                retries=0,
            )
            if not res.ok:
                fetch_failed = True
                detail = _short_error(res.stderr, "fetch failed")

        default_branch = (
            self._git.detect_default_branch(path, remote) if remote else None
        )
        ahead = 0
        behind = 0
        ab: tuple[int, int] | None = None
        if remote and default_branch:
            ref = "HEAD" if branch == default_branch else f"refs/heads/{default_branch}"
            ab = self._git.ahead_behind(
                path, ref, f"refs/remotes/{remote}/{default_branch}"
            )
            if ab is not None:
                ahead, behind = ab

        if fetch_failed:
            sync_state = StatusSyncState.ERROR
        elif not remotes:
            sync_state = StatusSyncState.LOCAL_ONLY
        elif remote is None or default_branch is None or ab is None:
            sync_state = StatusSyncState.NO_TRACKING
        elif ahead > 0 and behind > 0:
            sync_state = StatusSyncState.DIVERGED
        elif ahead > 0:
            sync_state = StatusSyncState.AHEAD
        elif behind > 0:
            sync_state = StatusSyncState.BEHIND
        else:
            sync_state = StatusSyncState.CLEAN

        has_issues = (
            tracked > 0
            or untracked > 0
            or sync_state
            in {
                StatusSyncState.AHEAD,
                StatusSyncState.BEHIND,
                StatusSyncState.DIVERGED,
                StatusSyncState.ERROR,
            }
        )

        return RepoStatusItem(
            name=repo.name,
            path=repo.path,
            root=repo.root,
            relative_path=repo.relative_path,
            branch=branch,
            default_branch=default_branch,
            tracked_dirty=tracked,
            untracked=untracked,
            ahead=ahead,
            behind=behind,
            remote_protocol=repo.remote_protocol,
            detail=detail,
            sync_state=sync_state,
            has_issues=has_issues,
        )

    def _sync_one(
        self,
        repo: DiscoveredRepo,
        *,
        preferred_remote: str,
        timeout_seconds: int,
        retries: int,
        prune: bool,
        autostash: bool,
        dry_run: bool,
        stop_event: Event,
    ) -> RepoSyncItem:
        start = time.monotonic()

        def finish(
            outcome: SyncOutcome,
            *,
            branch: str = "?",
            default_branch: str | None = None,
            ahead: int = 0,
            behind: int = 0,
            old_sha: str | None = None,
            new_sha: str | None = None,
            commits_pulled: int = 0,
            recent_commits: list[str] | None = None,
            stashed: bool = False,
            tracked_dirty: int = 0,
            untracked: int = 0,
            detail: str = "",
        ) -> RepoSyncItem:
            elapsed = int((time.monotonic() - start) * 1000)
            return RepoSyncItem(
                name=repo.name,
                path=repo.path,
                root=repo.root,
                relative_path=repo.relative_path,
                branch=branch,
                default_branch=default_branch,
                outcome=outcome,
                ahead=ahead,
                behind=behind,
                old_sha=old_sha,
                new_sha=new_sha,
                commits_pulled=commits_pulled,
                recent_commits=recent_commits or [],
                stashed=stashed,
                tracked_dirty=tracked_dirty,
                untracked=untracked,
                detail=detail,
                duration_ms=elapsed,
            )

        if stop_event.is_set():
            return finish(SyncOutcome.SKIPPED, detail="cancelled")

        path = Path(repo.path)
        if self._git.is_bare(path):
            return finish(
                SyncOutcome.SKIPPED, branch="(bare)", detail="bare repository"
            )

        branch = self._git.current_branch(path) or "(detached)"
        tracked, untracked = self._git.status_counts(path)
        remote = _choose_remote(self._git, path, preferred_remote)
        if remote is None:
            return finish(
                SyncOutcome.SKIPPED,
                branch=branch,
                tracked_dirty=tracked,
                untracked=untracked,
                detail="no usable remote configured",
            )

        fetch_res = self._git.fetch(
            path,
            remote,
            prune=prune,
            timeout_seconds=timeout_seconds,
            retries=retries,
        )
        if fetch_res.timed_out:
            return finish(
                SyncOutcome.TIMEOUT,
                branch=branch,
                tracked_dirty=tracked,
                untracked=untracked,
                detail=_short_error(fetch_res.stderr, "fetch timed out"),
            )
        if not fetch_res.ok:
            return finish(
                SyncOutcome.ERROR,
                branch=branch,
                tracked_dirty=tracked,
                untracked=untracked,
                detail=_short_error(fetch_res.stderr, "fetch failed"),
            )

        default_branch = self._git.detect_default_branch(path, remote)
        if default_branch is None:
            return finish(
                SyncOutcome.SKIPPED,
                branch=branch,
                tracked_dirty=tracked,
                untracked=untracked,
                detail="no default branch found on remote",
            )

        remote_ref = f"refs/remotes/{remote}/{default_branch}"
        remote_sha = self._git.rev_parse(path, remote_ref)
        if remote_sha is None:
            return finish(
                SyncOutcome.ERROR,
                branch=branch,
                default_branch=default_branch,
                tracked_dirty=tracked,
                untracked=untracked,
                detail=f"{remote}/{default_branch} could not be resolved",
            )

        if branch == default_branch:
            return self._sync_on_default(
                path=path,
                branch=branch,
                default_branch=default_branch,
                remote_ref=remote_ref,
                remote_sha=remote_sha,
                tracked=tracked,
                untracked=untracked,
                autostash=autostash,
                dry_run=dry_run,
                finish=finish,
            )

        return self._sync_off_default(
            path=path,
            branch=branch,
            default_branch=default_branch,
            remote=remote,
            remote_ref=remote_ref,
            remote_sha=remote_sha,
            tracked=tracked,
            untracked=untracked,
            timeout_seconds=timeout_seconds,
            dry_run=dry_run,
            finish=finish,
        )

    def _sync_on_default(
        self,
        *,
        path: Path,
        branch: str,
        default_branch: str,
        remote_ref: str,
        remote_sha: str,
        tracked: int,
        untracked: int,
        autostash: bool,
        dry_run: bool,
        finish: Callable[..., RepoSyncItem],
    ) -> RepoSyncItem:
        local_sha = self._git.rev_parse(path, "HEAD")
        if local_sha is None:
            return finish(
                SyncOutcome.SKIPPED,
                branch=branch,
                default_branch=default_branch,
                tracked_dirty=tracked,
                untracked=untracked,
                detail="unborn HEAD",
            )
        if local_sha == remote_sha:
            # Never touch or stash when already up to date.
            return finish(
                SyncOutcome.UP_TO_DATE,
                branch=branch,
                default_branch=default_branch,
                old_sha=local_sha[:8],
                new_sha=local_sha[:8],
                tracked_dirty=tracked,
                untracked=untracked,
            )

        ab = self._git.ahead_behind(path, "HEAD", remote_ref)
        if ab is None:
            return finish(
                SyncOutcome.ERROR,
                branch=branch,
                default_branch=default_branch,
                old_sha=local_sha[:8],
                tracked_dirty=tracked,
                untracked=untracked,
                detail="unable to compare HEAD with remote default branch",
            )
        ahead, behind = ab
        if ahead > 0 and behind == 0:
            return finish(
                SyncOutcome.AHEAD_LOCAL,
                branch=branch,
                default_branch=default_branch,
                ahead=ahead,
                behind=0,
                old_sha=local_sha[:8],
                new_sha=local_sha[:8],
                tracked_dirty=tracked,
                untracked=untracked,
                detail=f"{ahead} unpushed local commit(s)",
            )
        if ahead > 0 and behind > 0:
            return finish(
                SyncOutcome.DIVERGED,
                branch=branch,
                default_branch=default_branch,
                ahead=ahead,
                behind=behind,
                old_sha=local_sha[:8],
                tracked_dirty=tracked,
                untracked=untracked,
                detail=f"diverged ({ahead} ahead, {behind} behind)",
            )

        # ahead == 0 and behind > 0
        if dry_run:
            commits = self._git.log_oneline(path, f"{local_sha}..{remote_sha}", limit=5)
            return finish(
                SyncOutcome.WOULD_UPDATE,
                branch=branch,
                default_branch=default_branch,
                behind=behind,
                old_sha=local_sha[:8],
                new_sha=remote_sha[:8],
                commits_pulled=behind,
                recent_commits=commits,
                tracked_dirty=tracked,
                untracked=untracked,
                detail="dry-run",
            )

        # Try fast-forward directly first (succeeds cleanly when untracked/dirty
        # files do not collide with incoming changes — avoiding unnecessary stashes).
        merge_res = self._git.merge_ff_only(path, remote_ref)
        if merge_res.ok:
            new_sha = self._git.rev_parse(path, "HEAD") or remote_sha
            commits = self._git.log_oneline(path, f"{local_sha}..{new_sha}", limit=5)
            return finish(
                SyncOutcome.UPDATED,
                branch=branch,
                default_branch=default_branch,
                behind=behind,
                old_sha=local_sha[:8],
                new_sha=new_sha[:8],
                commits_pulled=behind,
                recent_commits=commits,
                tracked_dirty=tracked,
                untracked=untracked,
            )

        is_dirty = tracked > 0 or untracked > 0
        if not is_dirty:
            return finish(
                SyncOutcome.ERROR,
                branch=branch,
                default_branch=default_branch,
                behind=behind,
                old_sha=local_sha[:8],
                detail=_short_error(merge_res.stderr, "fast-forward merge failed"),
            )

        if not autostash:
            return finish(
                SyncOutcome.BLOCKED_DIRTY,
                branch=branch,
                default_branch=default_branch,
                behind=behind,
                old_sha=local_sha[:8],
                tracked_dirty=tracked,
                untracked=untracked,
                detail=(
                    f"fast-forward blocked by local changes "
                    f"({_short_error(merge_res.stderr, 'dirty working tree')}); "
                    "pass --autostash to stash and restore"
                ),
            )

        # Conditional AutoStash: only reached when behind > 0 AND merge was
        # actually blocked by local changes.
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        stash_label = f"repo-ffwd autostash {stamp}"
        stash_res = self._git.stash_push(path, stash_label)
        if not stash_res.ok:
            return finish(
                SyncOutcome.ERROR,
                branch=branch,
                default_branch=default_branch,
                behind=behind,
                old_sha=local_sha[:8],
                tracked_dirty=tracked,
                untracked=untracked,
                detail=_short_error(stash_res.stderr, "git stash push failed"),
            )

        merge_after = self._git.merge_ff_only(path, remote_ref)
        if not merge_after.ok:
            self._git.stash_pop(path)
            return finish(
                SyncOutcome.ERROR,
                branch=branch,
                default_branch=default_branch,
                behind=behind,
                old_sha=local_sha[:8],
                tracked_dirty=tracked,
                untracked=untracked,
                detail=_short_error(
                    merge_after.stderr, "fast-forward failed after stash"
                ),
            )

        new_sha = self._git.rev_parse(path, "HEAD") or remote_sha
        commits = self._git.log_oneline(path, f"{local_sha}..{new_sha}", limit=5)
        pop_res = self._git.stash_pop(path)
        if not pop_res.ok:
            return finish(
                SyncOutcome.STASH_CONFLICT,
                branch=branch,
                default_branch=default_branch,
                behind=behind,
                old_sha=local_sha[:8],
                new_sha=new_sha[:8],
                commits_pulled=behind,
                recent_commits=commits,
                stashed=True,
                tracked_dirty=tracked,
                untracked=untracked,
                detail=(
                    "fast-forwarded, but restoring stashed changes conflicted — "
                    f"stash preserved as {stash_label!r}"
                ),
            )

        return finish(
            SyncOutcome.UPDATED,
            branch=branch,
            default_branch=default_branch,
            behind=behind,
            old_sha=local_sha[:8],
            new_sha=new_sha[:8],
            commits_pulled=behind,
            recent_commits=commits,
            stashed=True,
            tracked_dirty=tracked,
            untracked=untracked,
        )

    def _sync_off_default(
        self,
        *,
        path: Path,
        branch: str,
        default_branch: str,
        remote: str,
        remote_ref: str,
        remote_sha: str,
        tracked: int,
        untracked: int,
        timeout_seconds: int,
        dry_run: bool,
        finish: Callable[..., RepoSyncItem],
    ) -> RepoSyncItem:
        # Protect any linked worktree that currently has the default branch checked out.
        checked_out_in = [
            wt
            for wt in self._git.worktree_branches(path).get(default_branch, [])
            if wt != path.resolve()
        ]
        if checked_out_in:
            return finish(
                SyncOutcome.SKIPPED,
                branch=branch,
                default_branch=default_branch,
                tracked_dirty=tracked,
                untracked=untracked,
                detail=(
                    f"on {branch}; {default_branch} checked out in worktree "
                    f"{checked_out_in[0]}"
                ),
            )

        local_default_ref = f"refs/heads/{default_branch}"
        local_default_sha = self._git.rev_parse(path, local_default_ref)
        if local_default_sha is None:
            return finish(
                SyncOutcome.SKIPPED,
                branch=branch,
                default_branch=default_branch,
                tracked_dirty=tracked,
                untracked=untracked,
                detail=f"on {branch}; no local {default_branch} branch",
            )
        if local_default_sha == remote_sha:
            return finish(
                SyncOutcome.UP_TO_DATE,
                branch=branch,
                default_branch=default_branch,
                old_sha=local_default_sha[:8],
                new_sha=local_default_sha[:8],
                tracked_dirty=tracked,
                untracked=untracked,
                detail=f"on {branch}; local {default_branch} up to date",
            )

        ab = self._git.ahead_behind(path, local_default_ref, remote_ref)
        if ab is None:
            return finish(
                SyncOutcome.ERROR,
                branch=branch,
                default_branch=default_branch,
                old_sha=local_default_sha[:8],
                tracked_dirty=tracked,
                untracked=untracked,
                detail=f"unable to compare local {default_branch} with {remote}/{default_branch}",
            )
        ahead, behind = ab
        if ahead > 0 and behind == 0:
            return finish(
                SyncOutcome.AHEAD_LOCAL,
                branch=branch,
                default_branch=default_branch,
                ahead=ahead,
                old_sha=local_default_sha[:8],
                new_sha=local_default_sha[:8],
                tracked_dirty=tracked,
                untracked=untracked,
                detail=f"on {branch}; local {default_branch} ahead by {ahead}",
            )
        if ahead > 0 and behind > 0:
            return finish(
                SyncOutcome.DIVERGED,
                branch=branch,
                default_branch=default_branch,
                ahead=ahead,
                behind=behind,
                old_sha=local_default_sha[:8],
                tracked_dirty=tracked,
                untracked=untracked,
                detail=f"on {branch}; local {default_branch} diverged (+{ahead}/-{behind})",
            )

        if dry_run:
            commits = self._git.log_oneline(
                path, f"{local_default_sha}..{remote_sha}", limit=5
            )
            return finish(
                SyncOutcome.WOULD_UPDATE,
                branch=branch,
                default_branch=default_branch,
                behind=behind,
                old_sha=local_default_sha[:8],
                new_sha=remote_sha[:8],
                commits_pulled=behind,
                recent_commits=commits,
                tracked_dirty=tracked,
                untracked=untracked,
                detail=f"dry-run; would fast-forward local {default_branch} ref",
            )

        refspec_res = self._git.fetch_refspec(
            path,
            remote,
            f"{default_branch}:{default_branch}",
            timeout_seconds=timeout_seconds,
        )
        if not refspec_res.ok:
            return finish(
                SyncOutcome.SKIPPED,
                branch=branch,
                default_branch=default_branch,
                behind=behind,
                old_sha=local_default_sha[:8],
                tracked_dirty=tracked,
                untracked=untracked,
                detail=(
                    f"on {branch}; ref-only fast-forward refused "
                    f"({_short_error(refspec_res.stderr, 'refused')})"
                ),
            )

        commits = self._git.log_oneline(
            path, f"{local_default_sha}..{remote_sha}", limit=5
        )
        return finish(
            SyncOutcome.UPDATED_OFF_BRANCH,
            branch=branch,
            default_branch=default_branch,
            behind=behind,
            old_sha=local_default_sha[:8],
            new_sha=remote_sha[:8],
            commits_pulled=behind,
            recent_commits=commits,
            tracked_dirty=tracked,
            untracked=untracked,
            detail=f"on {branch}; fast-forwarded local {default_branch} ref",
        )
