"""Hermetic end-to-end tests for RepoSyncService using real local git repos."""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

from repo_updater.core.adapters.git_cli import SubprocessGitAdapter
from repo_updater.core.models import GitExecResult, SyncOutcome
from repo_updater.core.services.repos import RepoSyncService
from repo_updater.core.settings import RootConfig

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
    assert "repo-updater autostash" in _git(clone, "stash", "list")


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
