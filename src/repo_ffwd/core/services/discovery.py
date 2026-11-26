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

"""Depth-bounded repository discovery with deduplication and exclude rules."""

from collections import deque
from dataclasses import dataclass
from pathlib import Path

import pathspec

from repo_ffwd.core.models import DiscoveredRepo, RemoteProtocol
from repo_ffwd.core.ports import GitPort
from repo_ffwd.core.settings import RootConfig


@dataclass
class DiscoveryResult:
    """Discovered repositories plus skipped linked worktrees and scanned roots."""

    repos: list[DiscoveredRepo]
    skipped_worktrees: list[str]
    roots_scanned: list[str]


def classify_remote_protocol(url: str | None) -> RemoteProtocol:
    """Classify a git remote URL into ``https``, ``ssh``, ``local``, or ``none``."""
    if not url:
        return "none"
    lowered = url.lower()
    if lowered.startswith(("https://", "http://")):
        return "https"
    if lowered.startswith("ssh://") or (
        "@" in url and ":" in url and not lowered.startswith("file://")
    ):
        return "ssh"
    return "local"


def _build_ignore_spec(
    root_dir: Path, global_excludes: list[str], root_excludes: list[str]
) -> pathspec.PathSpec | None:
    patterns: list[str] = [*global_excludes, *root_excludes]
    repoignore = root_dir / ".repoignore"
    if repoignore.is_file():
        try:
            patterns.extend(repoignore.read_text(encoding="utf-8").splitlines())
        except OSError:
            pass
    if not patterns:
        return None
    return pathspec.PathSpec.from_lines("gitignore", patterns)


def _is_excluded(spec: pathspec.PathSpec | None, rel_posix: str, name: str) -> bool:
    if spec is None:
        return False
    return (
        spec.match_file(rel_posix)
        or spec.match_file(f"{rel_posix}/")
        or spec.match_file(name)
        or spec.match_file(f"{name}/")
    )


def _select_remote(git: GitPort, repo_path: Path, preferred: str) -> str | None:
    remotes = git.list_remotes(repo_path)
    if not remotes:
        return None
    if preferred in remotes:
        return preferred
    if len(remotes) == 1:
        return remotes[0]
    return None


def discover_repositories(
    roots: list[RootConfig],
    *,
    git: GitPort,
    default_depth: int = 2,
    global_excludes: list[str] | None = None,
    include_worktrees: bool = False,
    tags: list[str] | None = None,
    preferred_remote: str = "origin",
) -> DiscoveryResult:
    """Discover git repositories across ``roots`` without scanning anything twice.

    - Breadth-first walk up to each root's ``depth`` (falling back to
      ``default_depth``).
    - Stops descending at any ``.git`` boundary so nested repos/submodules are
      never double-scanned.
    - Deduplicates by resolved realpath *and* (when ``include_worktrees=False``)
      by ``git --git-common-dir`` so overlapping roots, symlinks, and linked
      worktrees never trigger duplicate fetches.
    - Returns deterministic ordering by ``(root_index, relative_path)``.
    """
    excludes = list(global_excludes or [])
    tag_filter = set(tags or [])

    seen_realpaths: set[Path] = set()
    seen_common_dirs: set[Path] = set()
    discovered: list[tuple[int, str, DiscoveredRepo]] = []
    skipped_worktrees: list[str] = []
    roots_scanned: list[str] = []

    for root_idx, root_cfg in enumerate(roots):
        if tag_filter and not (tag_filter & set(root_cfg.tags)):
            continue
        root_dir = Path(root_cfg.path).expanduser().resolve()
        if not root_dir.is_dir():
            continue
        roots_scanned.append(str(root_dir))
        max_depth = root_cfg.depth if root_cfg.depth is not None else default_depth
        spec = _build_ignore_spec(root_dir, excludes, root_cfg.exclude)

        queue: deque[tuple[Path, int]] = deque([(root_dir, 0)])
        visited_walk: set[Path] = set()

        while queue:
            current, depth = queue.popleft()
            real = current.resolve()
            if real in visited_walk:
                continue
            visited_walk.add(real)

            dotgit = current / ".git"
            if dotgit.exists():
                is_worktree = dotgit.is_file()
                if is_worktree and not include_worktrees:
                    skipped_worktrees.append(str(real))
                    continue

                if real in seen_realpaths:
                    continue

                common_dir = git.git_common_dir(real)
                if (
                    not include_worktrees
                    and common_dir is not None
                    and common_dir in seen_common_dirs
                ):
                    skipped_worktrees.append(str(real))
                    continue

                seen_realpaths.add(real)
                if common_dir is not None:
                    seen_common_dirs.add(common_dir)

                rel = (
                    current.name
                    if current == root_dir
                    else current.relative_to(root_dir).as_posix()
                )
                remote_name = _select_remote(git, real, preferred_remote)
                remote_url = git.remote_url(real, remote_name) if remote_name else None
                repo_info = DiscoveredRepo(
                    path=str(real),
                    name=current.name,
                    root=str(root_dir),
                    relative_path=rel,
                    is_worktree=is_worktree,
                    common_dir=str(common_dir) if common_dir else "",
                    remote_url=remote_url,
                    remote_protocol=classify_remote_protocol(remote_url),
                    tags=list(root_cfg.tags),
                )
                discovered.append((root_idx, rel, repo_info))
                # Stop descending at .git boundary.
                continue

            if depth >= max_depth:
                continue

            try:
                children = sorted(current.iterdir(), key=lambda p: p.name)
            except OSError:
                continue

            for child in children:
                if not child.is_dir():
                    continue
                rel_posix = child.relative_to(root_dir).as_posix()
                if _is_excluded(spec, rel_posix, child.name):
                    continue
                queue.append((child, depth + 1))

    discovered.sort(key=lambda item: (item[0], item[1]))
    return DiscoveryResult(
        repos=[item[2] for item in discovered],
        skipped_worktrees=sorted(set(skipped_worktrees)),
        roots_scanned=roots_scanned,
    )
