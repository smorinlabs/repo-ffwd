# Copyright (c) 2025, Steve Morin
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

"""Driven ports for the projects capability (HEX-11).

A ``Protocol`` the application depends on; the HTTP adapter
(:class:`~repo_updater.core.adapters.py_api.PyApiProjectsRepository`)
and the in-memory fake
(:class:`~repo_updater.core.adapters.in_memory.InMemoryProjectsRepository`)
both satisfy it *structurally* — no inheritance required.

Methods are named for the domain verb and return domain models. ``None`` means
*absent* (no such id, unknown workspace name); a raised
:class:`~repo_updater.core.errors.APIError` means the upstream call
*failed*. The application service, not the adapter, decides that absence is a
user-facing error (HEX-12).
"""

from pathlib import Path
from typing import Protocol

from repo_updater.core.models import GitExecResult, Project


class ProjectsRepository(Protocol):
    """The projects upstream the application talks to."""

    def list_projects(
        self, *, workspace_gid: str | None, limit: int
    ) -> list[Project]: ...

    def get_project(self, project_id: str) -> Project | None: ...

    def resolve_workspace_gid(self, name: str) -> str | None: ...


class GitPort(Protocol):
    """Driven port for local and remote git operations."""

    def git_version(self) -> str | None: ...

    def is_bare(self, repo: Path) -> bool: ...

    def git_common_dir(self, repo: Path) -> Path | None: ...

    def list_remotes(self, repo: Path) -> list[str]: ...

    def remote_url(self, repo: Path, remote: str) -> str | None: ...

    def current_branch(self, repo: Path) -> str | None: ...

    def worktree_branches(self, repo: Path) -> dict[str, list[Path]]: ...

    def status_counts(self, repo: Path) -> tuple[int, int]: ...

    def fetch(
        self,
        repo: Path,
        remote: str,
        *,
        prune: bool,
        timeout_seconds: int,
        retries: int,
    ) -> GitExecResult: ...

    def detect_default_branch(self, repo: Path, remote: str) -> str | None: ...

    def rev_parse(self, repo: Path, rev: str) -> str | None: ...

    def ahead_behind(
        self, repo: Path, left_ref: str, right_ref: str
    ) -> tuple[int, int] | None: ...

    def merge_ff_only(self, repo: Path, target_ref: str) -> GitExecResult: ...

    def fetch_refspec(
        self, repo: Path, remote: str, refspec: str, *, timeout_seconds: int
    ) -> GitExecResult: ...

    def stash_push(self, repo: Path, message: str) -> GitExecResult: ...

    def stash_pop(self, repo: Path) -> GitExecResult: ...

    def log_oneline(
        self, repo: Path, rev_range: str, *, limit: int = 5
    ) -> list[str]: ...
