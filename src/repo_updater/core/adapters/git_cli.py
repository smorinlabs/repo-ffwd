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

"""Subprocess ``git`` adapter implementing :class:`~repo_updater.core.ports.GitPort`.

All invocations run strictly non-interactively (``stdin=DEVNULL``,
``GIT_TERMINAL_PROMPT=0``, ``GCM_INTERACTIVE=never``, ``LC_ALL=C``) with explicit
timeouts. Default ``GIT_SSH_COMMAND`` (``BatchMode=yes``) is injected **only**
when neither ``GIT_SSH_COMMAND``/``GIT_SSH`` nor repo ``core.sshCommand`` is set,
preserving custom SSH identities and proxies.
"""

import os
import shutil
import subprocess  # nosec B404 — git CLI subprocess wrapper
import time
from pathlib import Path

from repo_updater.core.models import GitExecResult

_DEFAULT_LOCAL_TIMEOUT = 15
_DEFAULT_SSH_COMMAND = "ssh -o BatchMode=yes -o ConnectTimeout=15"
_TRANSIENT_ERROR_MARKERS: tuple[str, ...] = (
    "could not resolve host",
    "temporary failure in name resolution",
    "failed to connect",
    "connection reset",
    "connection timed out",
    "remote end hung up unexpectedly",
    "early eof",
    "tls handshake timeout",
)


def _is_transient_network_error(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _TRANSIENT_ERROR_MARKERS)


class SubprocessGitAdapter:
    """Thread-safe subprocess adapter for ``git``."""

    def __init__(self, git_executable: str | None = None) -> None:
        self._git = git_executable or shutil.which("git") or "git"

    def _base_env(self, *, inject_ssh_batch: bool) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        env["LC_ALL"] = "C"
        if (
            inject_ssh_batch
            and not env.get("GIT_SSH_COMMAND")
            and not env.get("GIT_SSH")
        ):
            env["GIT_SSH_COMMAND"] = _DEFAULT_SSH_COMMAND
        return env

    def _has_repo_ssh_command(self, repo: Path) -> bool:
        res = self._run(
            repo,
            ["config", "--get", "core.sshCommand"],
            timeout=_DEFAULT_LOCAL_TIMEOUT,
            inject_ssh_batch=False,
        )
        return res.ok and bool(res.stdout.strip())

    def _run(
        self,
        repo: Path | None,
        args: list[str],
        *,
        timeout: int = _DEFAULT_LOCAL_TIMEOUT,
        inject_ssh_batch: bool = False,
    ) -> GitExecResult:
        argv = [self._git]
        if repo is not None:
            argv.extend(["-C", str(repo)])
        argv.extend(args)
        try:
            proc = subprocess.run(  # noqa: S603 # nosec B603
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=self._base_env(inject_ssh_batch=inject_ssh_batch),
                timeout=timeout,
                check=False,
            )
            return GitExecResult(
                returncode=proc.returncode,
                stdout=proc.stdout.strip(),
                stderr=proc.stderr.strip(),
            )
        except subprocess.TimeoutExpired:
            return GitExecResult(
                returncode=124,
                stderr=f"timed out after {timeout}s",
                timed_out=True,
            )
        except OSError as exc:
            return GitExecResult(returncode=127, stderr=str(exc))

    def git_version(self) -> str | None:
        res = self._run(None, ["--version"])
        return res.stdout if res.ok and res.stdout else None

    def is_bare(self, repo: Path) -> bool:
        res = self._run(repo, ["rev-parse", "--is-bare-repository"])
        return res.ok and res.stdout.lower() == "true"

    def git_common_dir(self, repo: Path) -> Path | None:
        res = self._run(
            repo, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
        )
        if not res.ok or not res.stdout:
            return None
        return Path(res.stdout).resolve()

    def list_remotes(self, repo: Path) -> list[str]:
        res = self._run(repo, ["remote"])
        if not res.ok or not res.stdout:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def remote_url(self, repo: Path, remote: str) -> str | None:
        res = self._run(repo, ["remote", "get-url", remote])
        return res.stdout if res.ok and res.stdout else None

    def current_branch(self, repo: Path) -> str | None:
        res = self._run(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
        if res.ok and res.stdout:
            return res.stdout
        return None

    def worktree_branches(self, repo: Path) -> dict[str, list[Path]]:
        """Map branch short name -> list of resolved worktree paths checking it out."""
        res = self._run(repo, ["worktree", "list", "--porcelain"])
        if not res.ok or not res.stdout:
            return {}
        branches: dict[str, list[Path]] = {}
        current_path: Path | None = None
        for line in res.stdout.splitlines():
            if line.startswith("worktree "):
                current_path = Path(line[len("worktree ") :].strip()).resolve()
            elif line.startswith("branch refs/heads/") and current_path is not None:
                branch = line[len("branch refs/heads/") :].strip()
                branches.setdefault(branch, []).append(current_path)
            elif not line.strip():
                current_path = None
        return branches

    def status_counts(self, repo: Path) -> tuple[int, int]:
        res = self._run(repo, ["status", "--porcelain"])
        if not res.ok or not res.stdout:
            return (0, 0)
        tracked = 0
        untracked = 0
        for line in res.stdout.splitlines():
            if line.startswith("?? "):
                untracked += 1
            elif line:
                tracked += 1
        return (tracked, untracked)

    def fetch(
        self,
        repo: Path,
        remote: str,
        *,
        prune: bool,
        timeout_seconds: int,
        retries: int,
    ) -> GitExecResult:
        inject_ssh = not self._has_repo_ssh_command(repo)
        args = ["fetch"]
        if prune:
            args.append("--prune")
        args.append(remote)

        attempts = max(1, retries + 1)
        last = GitExecResult(returncode=1)
        for attempt in range(attempts):
            last = self._run(
                repo, args, timeout=timeout_seconds, inject_ssh_batch=inject_ssh
            )
            if last.ok or last.timed_out:
                return last
            if attempt + 1 < attempts and _is_transient_network_error(last.stderr):
                time.sleep(0.5 * (attempt + 1))
                continue
            return last
        return last  # pragma: no cover — loop always returns

    def detect_default_branch(self, repo: Path, remote: str) -> str | None:
        sym = self._run(
            repo, ["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"]
        )
        prefix = f"{remote}/"
        if sym.ok and sym.stdout.startswith(prefix):
            return sym.stdout[len(prefix) :]
        for candidate in ("main", "master"):
            check = self._run(
                repo,
                [
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/remotes/{remote}/{candidate}",
                ],
            )
            if check.ok:
                return candidate
        return None

    def rev_parse(self, repo: Path, rev: str) -> str | None:
        res = self._run(repo, ["rev-parse", "--verify", "--quiet", rev])
        return res.stdout if res.ok and res.stdout else None

    def ahead_behind(
        self, repo: Path, left_ref: str, right_ref: str
    ) -> tuple[int, int] | None:
        res = self._run(
            repo, ["rev-list", "--left-right", "--count", f"{left_ref}...{right_ref}"]
        )
        if not res.ok or not res.stdout:
            return None
        parts = res.stdout.split()
        if len(parts) != 2:
            return None
        try:
            return (int(parts[0]), int(parts[1]))
        except ValueError:
            return None

    def merge_ff_only(self, repo: Path, target_ref: str) -> GitExecResult:
        return self._run(repo, ["merge", "--ff-only", target_ref])

    def fetch_refspec(
        self, repo: Path, remote: str, refspec: str, *, timeout_seconds: int
    ) -> GitExecResult:
        inject_ssh = not self._has_repo_ssh_command(repo)
        return self._run(
            repo,
            ["fetch", remote, refspec],
            timeout=timeout_seconds,
            inject_ssh_batch=inject_ssh,
        )

    def stash_push(self, repo: Path, message: str) -> GitExecResult:
        return self._run(repo, ["stash", "push", "--include-untracked", "-m", message])

    def stash_pop(self, repo: Path) -> GitExecResult:
        return self._run(repo, ["stash", "pop"])

    def log_oneline(self, repo: Path, rev_range: str, *, limit: int = 5) -> list[str]:
        res = self._run(repo, ["log", "--oneline", "-n", str(limit), rev_range])
        if not res.ok or not res.stdout:
            return []
        return res.stdout.splitlines()
