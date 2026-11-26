"""Hermetic end-to-end tests for RepoSyncService using real local git repos."""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

from repo_ffwd.core.adapters.git_cli import SubprocessGitAdapter
from repo_ffwd.core.models import (
    GitExecResult,
    RepoStatusItem,
    RepoStatusReport,
    StatusFilter,
    StatusSyncState,
    SyncOutcome,
)
from repo_ffwd.core.services.repos import RepoSyncService
from repo_ffwd.core.settings import RootConfig

_GIT = shutil.which("git") or "git"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603
        [_GIT, "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _make_remote_and_clone(
    tmp_path: Path, name: str, *, default_branch: str = "main"
) -> tuple[Path, Path, Path]:
    """Create (bare_remote, upstream_work, local_clone) with one initial commit."""
    bare = tmp_path / "remotes" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        [_GIT, "init", "--bare", "-b", default_branch, str(bare)],
        check=True,
        capture_output=True,
    )

    upstream = tmp_path / "upstream" / name
    subprocess.run(  # noqa: S603
        [_GIT, "clone", str(bare), str(upstream)],
        check=True,
        capture_output=True,
    )
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "Test User")
    (upstream / "README.md").write_text("v1\n", encoding="utf-8")
    _git(upstream, "add", "README.md")
    _git(upstream, "commit", "-m", "initial commit")
    _git(upstream, "push", "-u", "origin", default_branch)

    clone = tmp_path / "workspace" / name
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        [_GIT, "clone", str(bare), str(clone)],
        check=True,
        capture_output=True,
    )
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test User")
    return bare, upstream, clone


def _push_upstream_commit(
    upstream: Path, filename: str, content: str, message: str
) -> str:
    (upstream / filename).write_text(content, encoding="utf-8")
    _git(upstream, "add", filename)
    _git(upstream, "commit", "-m", message)
    _git(upstream, "push")
    return _git(upstream, "rev-parse", "HEAD")


def test_sync_up_to_date_dirty_never_stashes_even_with_autostash(tmp_path):
    _, _, clone = _make_remote_and_clone(tmp_path, "clean-dirty")
    (clone / "README.md").write_text("local edit\n", encoding="utf-8")
    (clone / "notes.txt").write_text("untracked\n", encoding="utf-8")

    svc = RepoSyncService(SubprocessGitAdapter())
    report = svc.sync_repos(
        [RootConfig(path=str(tmp_path / "workspace"))],
        autostash=True,
    )
    assert len(report.items) == 1
    item = report.items[0]
    assert item.outcome is SyncOutcome.UP_TO_DATE
    assert item.stashed is False
    assert _git(clone, "stash", "list") == ""
    assert (clone / "README.md").read_text(encoding="utf-8") == "local edit\n"
    assert (clone / "notes.txt").read_text(encoding="utf-8") == "untracked\n"


def test_sync_fast_forwards_and_records_commits(tmp_path):
    _, upstream, clone = _make_remote_and_clone(tmp_path, "ff-repo")
    _push_upstream_commit(upstream, "a.txt", "a\n", "feat: add a")
    tip = _push_upstream_commit(upstream, "b.txt", "b\n", "feat: add b")
    # Non-colliding untracked file should survive without stashing.
    (clone / "local.tmp").write_text("keep me\n", encoding="utf-8")

    svc = RepoSyncService(SubprocessGitAdapter())
    report = svc.sync_repos([RootConfig(path=str(tmp_path / "workspace"))])
    assert not report.has_failures()
    item = report.items[0]
    assert item.outcome is SyncOutcome.UPDATED
    assert item.commits_pulled == 2
    assert item.new_sha == tip[:8]
    assert item.stashed is False
    assert len(item.recent_commits) == 2
    assert (clone / "local.tmp").read_text(encoding="utf-8") == "keep me\n"


def test_sync_dirty_collision_blocks_by_default_and_autostash_restores(tmp_path):
    _, upstream, clone = _make_remote_and_clone(tmp_path, "dirty-repo")
    # Upstream modifies README.md (line 1) and adds another file.
    _push_upstream_commit(upstream, "upstream.txt", "u\n", "feat: add upstream file")
    _push_upstream_commit(upstream, "README.md", "v1\nline2\n", "docs: add line2")

    # Local modifies README.md on a non-conflicting hunk (or wait: modify tracked file).
    # First test blocking when autostash=False.
    (clone / "README.md").write_text("v1\n", encoding="utf-8")
    (clone / "upstream.txt").write_text("untracked collision\n", encoding="utf-8")

    svc = RepoSyncService(SubprocessGitAdapter())
    blocked = svc.sync_repos(
        [RootConfig(path=str(tmp_path / "workspace"))],
        autostash=False,
    )
    assert blocked.has_failures()
    assert blocked.items[0].outcome is SyncOutcome.BLOCKED_DIRTY
    assert _git(clone, "stash", "list") == ""

    # Replace colliding untracked file with a clean tracked edit on a separate file
    # plus a colliding edit on a tracked file so stash push+pop restores cleanly.
    (clone / "upstream.txt").unlink()
    (clone / "README.md").write_text("v1\nline2\nlocal-extra\n", encoding="utf-8")
    (clone / "extra.txt").write_text("mine\n", encoding="utf-8")
    # Reset README so merge collides on README, then stash+pop cleanly?
    # Actually: if README changed in both upstream ("v1\nline2\n") and local
    # ("v1\nlocal-only\n"), pop will conflict; test clean restore using a
    # non-overlapping file first!
    (clone / "README.md").write_text("v1\n", encoding="utf-8")
    # Make README have 5 lines in base so local edit at bottom + upstream edit at top merge cleanly.
    _, upstream2, clone2 = _make_remote_and_clone(tmp_path, "autostash-clean")
    _push_upstream_commit(
        upstream2, "doc.txt", "L1\nL2\nL3\nL4\nL5\n", "docs: add 5 lines"
    )
    _git(clone2, "pull", "--ff-only")
    _push_upstream_commit(
        upstream2, "doc.txt", "L1-upstream\nL2\nL3\nL4\nL5\n", "docs: edit line 1"
    )
    (clone2 / "doc.txt").write_text("L1\nL2\nL3\nL4\nL5-local\n", encoding="utf-8")

    res = svc.sync_repos(
        [RootConfig(path=str(clone2))],
        autostash=True,
    )
    assert not res.has_failures()
    assert res.items[0].outcome is SyncOutcome.UPDATED
    assert res.items[0].stashed is True
    assert _git(clone2, "stash", "list") == ""
    assert (clone2 / "doc.txt").read_text(
        encoding="utf-8"
    ) == "L1-upstream\nL2\nL3\nL4\nL5-local\n"


def test_sync_autostash_conflict_preserves_stash(tmp_path):
    _, upstream, clone = _make_remote_and_clone(tmp_path, "autostash-conflict")
    _push_upstream_commit(upstream, "README.md", "upstream-change\n", "docs: upstream")
    (clone / "README.md").write_text("local-conflicting-change\n", encoding="utf-8")

    svc = RepoSyncService(SubprocessGitAdapter())
    report = svc.sync_repos(
        [RootConfig(path=str(tmp_path / "workspace"))],
        autostash=True,
    )
    assert report.has_failures()
    item = report.items[0]
    assert item.outcome is SyncOutcome.STASH_CONFLICT
    assert item.stashed is True
    assert "repo-ffwd autostash" in _git(clone, "stash", "list")


def test_sync_ahead_and_diverged_untouched(tmp_path):
    _, _, ahead_clone = _make_remote_and_clone(tmp_path, "ahead")
    (ahead_clone / "local.txt").write_text("local\n", encoding="utf-8")
    _git(ahead_clone, "add", "local.txt")
    _git(ahead_clone, "commit", "-m", "local commit")

    _, div_up, div_clone = _make_remote_and_clone(tmp_path, "diverged")
    _push_upstream_commit(div_up, "up.txt", "up\n", "upstream commit")
    (div_clone / "mine.txt").write_text("mine\n", encoding="utf-8")
    _git(div_clone, "add", "mine.txt")
    _git(div_clone, "commit", "-m", "local diverged commit")

    svc = RepoSyncService(SubprocessGitAdapter())
    report = svc.sync_repos([RootConfig(path=str(tmp_path / "workspace"))])
    by_name = {i.name: i for i in report.items}
    assert by_name["ahead"].outcome is SyncOutcome.AHEAD_LOCAL
    assert by_name["ahead"].ahead == 1
    assert by_name["diverged"].outcome is SyncOutcome.DIVERGED
    assert by_name["diverged"].ahead == 1
    assert by_name["diverged"].behind == 1


def test_sync_off_branch_updates_ref_and_protects_linked_worktree(tmp_path):
    _, upstream, clone = _make_remote_and_clone(tmp_path, "off-branch")
    _git(clone, "switch", "-c", "feat/work")
    tip = _push_upstream_commit(upstream, "new.txt", "new\n", "feat: upstream")

    svc = RepoSyncService(SubprocessGitAdapter())
    report = svc.sync_repos([RootConfig(path=str(tmp_path / "workspace"))])
    item = report.items[0]
    assert item.outcome is SyncOutcome.UPDATED_OFF_BRANCH
    assert _git(clone, "symbolic-ref", "--short", "HEAD") == "feat/work"
    assert _git(clone, "rev-parse", "refs/heads/main") == tip

    # Now check out main in a linked worktree and push another upstream commit:
    # the off-branch main clone MUST skip updating refs/heads/main to avoid
    # desynchronizing the linked worktree's index.
    wt = tmp_path / "workspace" / "off-branch-main-wt"
    _git(clone, "worktree", "add", str(wt), "main")
    _push_upstream_commit(upstream, "new2.txt", "new2\n", "feat: upstream 2")

    report2 = svc.sync_repos([RootConfig(path=str(tmp_path / "workspace"))])
    assert len(report2.items) == 1
    assert report2.items[0].outcome is SyncOutcome.SKIPPED
    assert "worktree" in report2.items[0].detail
    assert _git(clone, "rev-parse", "refs/heads/main") == tip


def test_sync_dry_run_does_not_mutate_head(tmp_path):
    _, upstream, clone = _make_remote_and_clone(tmp_path, "dry-run")
    before = _git(clone, "rev-parse", "HEAD")
    _push_upstream_commit(upstream, "x.txt", "x\n", "feat: x")

    svc = RepoSyncService(SubprocessGitAdapter())
    report = svc.sync_repos(
        [RootConfig(path=str(tmp_path / "workspace"))], dry_run=True
    )
    assert report.items[0].outcome is SyncOutcome.WOULD_UPDATE
    assert report.items[0].commits_pulled == 1
    assert _git(clone, "rev-parse", "HEAD") == before


def test_sync_parallel_order_and_status(tmp_path):
    names = ["zeta", "alpha", "mid", "beta"]
    for name in names:
        _, upstream, _ = _make_remote_and_clone(tmp_path, name)
        _push_upstream_commit(upstream, f"{name}.txt", name, f"feat: {name}")

    svc = RepoSyncService(SubprocessGitAdapter())
    status_before = svc.status_repos(
        [RootConfig(path=str(tmp_path / "workspace"))], fetch=True, concurrency=4
    )
    assert [i.name for i in status_before.items] == sorted(names)
    assert all(i.behind == 1 for i in status_before.items)

    report = svc.sync_repos(
        [RootConfig(path=str(tmp_path / "workspace"))], concurrency=4
    )
    assert [i.name for i in report.items] == sorted(names)
    assert all(i.outcome is SyncOutcome.UPDATED for i in report.items)


def test_sync_timeout_and_ssh_transport_preservation(tmp_path):
    _, _, clone = _make_remote_and_clone(tmp_path, "timeout-repo")
    adapter = SubprocessGitAdapter()

    # Verify core.sshCommand suppresses default GIT_SSH_COMMAND injection.
    _git(clone, "config", "core.sshCommand", "ssh -i /custom/key")
    assert adapter._has_repo_ssh_command(clone) is True
    env_no_inject = adapter._base_env(inject_ssh_batch=False)
    assert env_no_inject["GIT_TERMINAL_PROMPT"] == "0"
    assert env_no_inject["GCM_INTERACTIVE"] == "never"

    with patch.object(
        adapter,
        "fetch",
        return_value=GitExecResult(
            returncode=124, stderr="timed out after 1s", timed_out=True
        ),
    ):
        svc = RepoSyncService(adapter)
        report = svc.sync_repos(
            [RootConfig(path=str(tmp_path / "workspace"))], timeout_seconds=1
        )
    assert report.has_failures()
    assert report.items[0].outcome is SyncOutcome.TIMEOUT


def test_status_sync_states_has_issues_and_filters(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # 1. Clean repo
    _make_remote_and_clone(tmp_path, "r-clean")

    # 2. Dirty repo (tracked edit + untracked file)
    _, _, dirty_clone = _make_remote_and_clone(tmp_path, "r-dirty")
    (dirty_clone / "README.md").write_text("modified\n", encoding="utf-8")
    (dirty_clone / "extra.txt").write_text("untracked\n", encoding="utf-8")

    # 3. Ahead repo
    _, _, ahead_clone = _make_remote_and_clone(tmp_path, "r-ahead")
    (ahead_clone / "a.txt").write_text("ahead\n", encoding="utf-8")
    _git(ahead_clone, "add", "a.txt")
    _git(ahead_clone, "commit", "-m", "local ahead")

    # 4. Behind repo (fetched so origin/main is ahead locally)
    _, behind_up, behind_clone = _make_remote_and_clone(tmp_path, "r-behind")
    _push_upstream_commit(behind_up, "b.txt", "b\n", "upstream behind")
    _git(behind_clone, "fetch", "origin")

    # 5. Diverged repo
    _, div_up, div_clone = _make_remote_and_clone(tmp_path, "r-diverged")
    _push_upstream_commit(div_up, "u.txt", "u\n", "upstream commit")
    (div_clone / "l.txt").write_text("l\n", encoding="utf-8")
    _git(div_clone, "add", "l.txt")
    _git(div_clone, "commit", "-m", "local commit")
    _git(div_clone, "fetch", "origin")

    # 6. Local-only repo (no remotes)
    local_repo = workspace / "r-local"
    local_repo.mkdir()
    _git(local_repo, "init", "-b", "main")
    _git(local_repo, "config", "user.email", "test@example.com")
    _git(local_repo, "config", "user.name", "Test User")
    (local_repo / "file.txt").write_text("local\n", encoding="utf-8")
    _git(local_repo, "add", "file.txt")
    _git(local_repo, "commit", "-m", "init local")

    # 7. No-tracking repo (has a remote, never fetched so no remote refs)
    notrack_repo = workspace / "r-notracking"
    notrack_repo.mkdir()
    _git(notrack_repo, "init", "-b", "main")
    _git(notrack_repo, "config", "user.email", "test@example.com")
    _git(notrack_repo, "config", "user.name", "Test User")
    (notrack_repo / "file.txt").write_text("nt\n", encoding="utf-8")
    _git(notrack_repo, "add", "file.txt")
    _git(notrack_repo, "commit", "-m", "init notracking")
    _git(notrack_repo, "remote", "add", "origin", "https://example.invalid/repo.git")

    svc = RepoSyncService(SubprocessGitAdapter())
    root = [RootConfig(path=str(workspace))]
    full = svc.status_repos(root)
    by_name = {i.name: i for i in full.items}

    assert by_name["r-clean"].sync_state is StatusSyncState.CLEAN
    assert by_name["r-clean"].has_issues is False

    assert by_name["r-dirty"].sync_state is StatusSyncState.CLEAN
    assert by_name["r-dirty"].tracked_dirty == 1
    assert by_name["r-dirty"].untracked == 1
    assert by_name["r-dirty"].has_issues is True

    assert by_name["r-ahead"].sync_state is StatusSyncState.AHEAD
    assert by_name["r-ahead"].ahead == 1
    assert by_name["r-ahead"].has_issues is True

    assert by_name["r-behind"].sync_state is StatusSyncState.BEHIND
    assert by_name["r-behind"].behind == 1
    assert by_name["r-behind"].has_issues is True

    assert by_name["r-diverged"].sync_state is StatusSyncState.DIVERGED
    assert by_name["r-diverged"].ahead == 1
    assert by_name["r-diverged"].behind == 1
    assert by_name["r-diverged"].has_issues is True

    assert by_name["r-local"].sync_state is StatusSyncState.LOCAL_ONLY
    assert by_name["r-local"].has_issues is False

    assert by_name["r-notracking"].sync_state is StatusSyncState.NO_TRACKING
    assert by_name["r-notracking"].has_issues is False

    counts = full.summary_counts()
    assert counts == {
        "total": 7,
        "dirty": 1,
        "ahead": 1,
        "behind": 1,
        "diverged": 1,
        "local": 1,
        "errors": 0,
    }

    # Single filters
    assert [
        i.name for i in svc.status_repos(root, filters=[StatusFilter.DIRTY]).items
    ] == ["r-dirty"]
    assert [
        i.name for i in svc.status_repos(root, filters=[StatusFilter.CLEAN]).items
    ] == ["r-clean"]
    assert [
        i.name for i in svc.status_repos(root, filters=[StatusFilter.AHEAD]).items
    ] == [
        "r-ahead",
        "r-diverged",
    ]
    assert [
        i.name for i in svc.status_repos(root, filters=[StatusFilter.BEHIND]).items
    ] == [
        "r-behind",
        "r-diverged",
    ]
    assert [
        i.name for i in svc.status_repos(root, filters=[StatusFilter.DIVERGED]).items
    ] == ["r-diverged"]
    assert [
        i.name for i in svc.status_repos(root, filters=[StatusFilter.LOCAL]).items
    ] == ["r-local"]
    assert [
        i.name for i in svc.status_repos(root, filters=[StatusFilter.ISSUES]).items
    ] == [
        "r-ahead",
        "r-behind",
        "r-dirty",
        "r-diverged",
    ]

    # Repeatable OR filter
    combo = svc.status_repos(root, filters=[StatusFilter.DIRTY, StatusFilter.LOCAL])
    assert [i.name for i in combo.items] == ["r-dirty", "r-local"]


def test_status_fetch_error_marks_error_state(tmp_path):
    _make_remote_and_clone(tmp_path, "err-repo")
    adapter = SubprocessGitAdapter()
    with patch.object(
        adapter,
        "fetch",
        return_value=GitExecResult(returncode=128, stderr="fatal: network unreachable"),
    ):
        svc = RepoSyncService(adapter)
        report = svc.status_repos(
            [RootConfig(path=str(tmp_path / "workspace"))], fetch=True
        )
    assert len(report.items) == 1
    item = report.items[0]
    assert item.sync_state is StatusSyncState.ERROR
    assert item.has_issues is True
    assert "network unreachable" in item.detail


def test_status_report_table_and_detailed_formatting():
    items = [
        RepoStatusItem(
            name="clean",
            path="/ws/clean",
            root="/ws",
            relative_path="clean",
            branch="main",
            default_branch="main",
            remote_protocol="https",
            sync_state=StatusSyncState.CLEAN,
            has_issues=False,
        ),
        RepoStatusItem(
            name="ahead",
            path="/ws/ahead",
            root="/ws",
            relative_path="ahead",
            branch="main",
            default_branch="main",
            ahead=2,
            remote_protocol="ssh",
            sync_state=StatusSyncState.AHEAD,
            has_issues=True,
        ),
        RepoStatusItem(
            name="behind",
            path="/ws/behind",
            root="/ws",
            relative_path="behind",
            branch="main",
            default_branch="main",
            behind=3,
            remote_protocol="ssh",
            sync_state=StatusSyncState.BEHIND,
            has_issues=True,
        ),
        RepoStatusItem(
            name="div",
            path="/ws/div",
            root="/ws",
            relative_path="div",
            branch="main",
            default_branch="main",
            tracked_dirty=1,
            untracked=2,
            ahead=1,
            behind=4,
            remote_protocol="https",
            sync_state=StatusSyncState.DIVERGED,
            has_issues=True,
        ),
        RepoStatusItem(
            name="loc",
            path="/ws/loc",
            root="/ws",
            relative_path="loc",
            branch="main",
            remote_protocol="none",
            sync_state=StatusSyncState.LOCAL_ONLY,
            has_issues=False,
        ),
        RepoStatusItem(
            name="nt",
            path="/ws/nt",
            root="/ws",
            relative_path="nt",
            branch="feat",
            remote_protocol="https",
            sync_state=StatusSyncState.NO_TRACKING,
            has_issues=False,
        ),
        RepoStatusItem(
            name="err",
            path="/ws/err",
            root="/ws",
            relative_path="err",
            branch="main",
            remote_protocol="https",
            detail="auth failed",
            sync_state=StatusSyncState.ERROR,
            has_issues=True,
        ),
    ]
    report = RepoStatusReport(items=items, detailed=False)
    assert "Sync" in report.table_columns()
    sync_idx = report.table_columns().index("Sync")

    plain_sync = [row[sync_idx] for row in report.table_rows()]
    assert plain_sync == [
        "clean",
        "ahead +2",
        "behind -3",
        "diverged +1/-4",
        "local",
        "-",
        "error",
    ]
    rich_sync = [row[sync_idx] for row in report.table_rows_rich()]
    assert rich_sync == [
        "[green]✓[/green]",
        "[yellow]↑2[/yellow]",
        "[red]↓3[/red]",
        "[red]↕+1/-4[/red]",
        "[blue]L[/blue]",
        "[dim]-[/dim]",
        "[red]✗[/red]",
    ]
    assert report.render_text() is None
    assert report.render_markdown() is None

    detailed = RepoStatusReport(items=items, detailed=True)
    text_out = detailed.render_text()
    md_out = detailed.render_markdown()
    assert text_out is not None
    assert md_out is not None
    assert "Diverged (+1 ahead, -4 behind)" in text_out
    assert "auth failed" in text_out
    assert "### div" in md_out
    assert "1 dirty tracked, 2 untracked" in md_out
    assert "Error: auth failed" in md_out

    empty_filtered = RepoStatusReport(items=[], filters=[StatusFilter.DIRTY])
    assert empty_filtered.human_note() == "No repositories match filter: dirty."
