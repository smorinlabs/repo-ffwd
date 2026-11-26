"""Tests for depth-bounded repository discovery, worktree filtering, and dedup."""

import shutil
import subprocess
from pathlib import Path

from repo_ffwd.core.adapters.git_cli import SubprocessGitAdapter
from repo_ffwd.core.services.discovery import (
    classify_remote_protocol,
    discover_repositories,
)
from repo_ffwd.core.settings import RootConfig

_GIT = shutil.which("git") or "git"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        [_GIT, "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path, remote_url: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")
    if remote_url:
        _git(path, "remote", "add", "origin", remote_url)
    return path


def test_classify_remote_protocol():
    assert classify_remote_protocol("https://github.com/org/repo.git") == "https"
    assert classify_remote_protocol("http://example.com/repo.git") == "https"
    assert classify_remote_protocol("git@github.com:org/repo.git") == "ssh"
    assert classify_remote_protocol("ssh://git@github.com/org/repo.git") == "ssh"
    assert classify_remote_protocol("/srv/bare.git") == "local"
    assert classify_remote_protocol("file:///srv/bare.git") == "local"
    assert classify_remote_protocol(None) == "none"


def test_discovery_respects_depth_and_stops_at_git_boundary(tmp_path):
    git = SubprocessGitAdapter()
    top = _init_repo(tmp_path / "alpha", "https://example.com/alpha.git")
    # Nested git repo inside alpha must NOT be discovered (stop at .git boundary).
    _init_repo(top / "nested-inside-alpha")
    # Depth-2 repo under a group dir.
    _init_repo(tmp_path / "group" / "beta", "git@example.com:org/beta.git")
    # Depth-3 repo beyond default depth=2.
    _init_repo(tmp_path / "group" / "sub" / "gamma")

    res_d1 = discover_repositories(
        [RootConfig(path=str(tmp_path))], git=git, default_depth=1
    )
    assert [r.relative_path for r in res_d1.repos] == ["alpha"]

    res_d2 = discover_repositories(
        [RootConfig(path=str(tmp_path))], git=git, default_depth=2
    )
    assert [r.relative_path for r in res_d2.repos] == ["alpha", "group/beta"]
    assert res_d2.repos[0].remote_protocol == "https"
    assert res_d2.repos[1].remote_protocol == "ssh"


def test_discovery_skips_worktrees_by_default_and_deduplicates_overlapping_roots(
    tmp_path,
):
    git = SubprocessGitAdapter()
    main_repo = _init_repo(tmp_path / "proj")
    wt_path = tmp_path / "proj-wt"
    _git(main_repo, "worktree", "add", "-b", "feat/x", str(wt_path))

    # Symlink pointing to the same repo should not be scanned twice.
    link_path = tmp_path / "proj-link"
    link_path.symlink_to(main_repo)

    res = discover_repositories(
        [RootConfig(path=str(tmp_path)), RootConfig(path=str(main_repo))],
        git=git,
        default_depth=2,
        include_worktrees=False,
    )
    assert [r.name for r in res.repos] == ["proj"]
    assert str(wt_path.resolve()) in res.skipped_worktrees

    res_inc = discover_repositories(
        [RootConfig(path=str(tmp_path))],
        git=git,
        default_depth=2,
        include_worktrees=True,
    )
    assert sorted(r.name for r in res_inc.repos) == ["proj", "proj-wt"]
    by_name = {r.name: r for r in res_inc.repos}
    assert by_name["proj"].is_worktree is False
    assert by_name["proj-wt"].is_worktree is True


def test_discovery_honors_excludes_repoignore_and_tags(tmp_path):
    git = SubprocessGitAdapter()
    _init_repo(tmp_path / "keep")
    _init_repo(tmp_path / "_archive" / "old")
    _init_repo(tmp_path / "ignored-by-file")
    _init_repo(tmp_path / "ignored-by-config")
    (tmp_path / ".repoignore").write_text("ignored-by-file/\n", encoding="utf-8")

    roots = [
        RootConfig(
            path=str(tmp_path),
            exclude=["ignored-by-config"],
            tags=["primary"],
        )
    ]
    res = discover_repositories(
        roots,
        git=git,
        default_depth=2,
        global_excludes=["_archive"],
    )
    assert [r.name for r in res.repos] == ["keep"]

    filtered_miss = discover_repositories(
        roots, git=git, default_depth=2, tags=["other"]
    )
    assert filtered_miss.repos == []

    filtered_hit = discover_repositories(
        roots,
        git=git,
        default_depth=2,
        global_excludes=["_archive"],
        tags=["primary"],
    )
    assert [r.name for r in filtered_hit.repos] == ["keep"]
