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

"""Result models — the single source of truth for every command's output.

Each command returns one of these Pydantic models. The CLI renderer turns the
*same* object into human text, JSON, or Markdown; the future web service will
return it as a JSON response. Because the model is the contract, the JSON
representation of every command is defined here, in one place.

To add a new result type: subclass :class:`CLIResult` and implement
``table_columns``/``table_rows`` (used by the human and Markdown renderers).
JSON rendering is automatic via Pydantic's ``model_dump_json``.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal, assert_never, override

from pydantic import BaseModel, Field

from repo_ffwd.core.format import rich_link


class CLIResult(BaseModel):
    """Base class for renderable command results.

    Subclasses describe how they tabulate for the human/Markdown renderers.
    The JSON renderer ignores these helpers and serializes the model fields.
    """

    def table_title(self) -> str | None:
        """Optional heading shown above the table (human/Markdown)."""
        return None

    def table_columns(self) -> list[str]:
        """Column headers. Empty means "no table" (renderer falls back to note)."""
        return []

    def table_rows(self) -> list[list[str]]:
        """Row cells as strings, aligned with :meth:`table_columns`."""
        return []

    def table_rows_rich(self) -> list[list[str]]:
        """Row cells for the *text* renderer only — may carry rich markup.

        Override to add terminal niceties (OSC-8 hyperlinks via
        :func:`core.format.rich_link`, relative times via
        :func:`core.format.relative_time`). Defaults to :meth:`table_rows`;
        Markdown and JSON always use the plain representations.
        """
        return self.table_rows()

    def human_note(self) -> str | None:
        """Optional plain message shown when there is nothing tabular to show."""
        return None

    def render_text(self) -> str | None:
        """Optional custom Rich-markup text view, bypassing table rendering."""
        return None

    def render_markdown(self) -> str | None:
        """Optional custom Markdown view, bypassing Markdown table rendering."""
        return None


class Project(BaseModel):
    """A single Py project."""

    id: str
    name: str
    workspace: str | None = None


class ProjectList(CLIResult):
    """A collection of projects."""

    projects: list[Project]

    @override
    def table_title(self) -> str | None:
        return f"Projects ({len(self.projects)})"

    @override
    def table_columns(self) -> list[str]:
        return ["Name", "Workspace", "ID"]

    @override
    def table_rows(self) -> list[list[str]]:
        return [[p.name, p.workspace or "-", p.id] for p in self.projects]

    @override
    def human_note(self) -> str | None:
        return "No projects found." if not self.projects else None


class ConfigValue(CLIResult):
    """A single resolved configuration value and where it came from."""

    key: str
    value: str | None = None
    source: str | None = None

    @override
    def table_columns(self) -> list[str]:
        return ["Key", "Value", "Source"]

    @override
    def table_rows(self) -> list[list[str]]:
        return [
            [self.key, self.value if self.value is not None else "", self.source or "-"]
        ]


class ConfigPath(CLIResult):
    """The location of the config file and whether it exists on disk."""

    path: str
    exists: bool

    @override
    def table_columns(self) -> list[str]:
        return ["Config path", "Exists"]

    @override
    def table_rows(self) -> list[list[str]]:
        return [[self.path, "yes" if self.exists else "no"]]

    @override
    def table_rows_rich(self) -> list[list[str]]:
        try:
            uri = Path(self.path).as_uri()
        except ValueError:  # relative path — no sensible file:// form
            return self.table_rows()
        return [[rich_link(self.path, uri), "yes" if self.exists else "no"]]


type CheckStatus = Literal["ok", "warn", "error"]


class DoctorCheck(BaseModel):
    """One diagnostic check result."""

    name: str
    status: CheckStatus
    detail: str


class DoctorReport(CLIResult):
    """Aggregated diagnostics for `repo_ffwd doctor`."""

    checks: list[DoctorCheck]

    @override
    def table_title(self) -> str | None:
        return "Diagnostics"

    @override
    def table_columns(self) -> list[str]:
        return ["Check", "Status", "Detail"]

    @override
    def table_rows(self) -> list[list[str]]:
        return [[c.name, c.status, c.detail] for c in self.checks]

    def has_error(self) -> bool:
        return any(c.status == "error" for c in self.checks)


class DiagnosticsBundle(CLIResult):
    """Redacted environment snapshot for bug reports (`doctor --bundle`).

    Everything a maintainer needs to reproduce a setup problem, nothing the
    user would regret pasting into a public issue: secrets are redacted at
    collection time (see ``diagnostics.build_bundle``), and log *contents*
    are deliberately excluded — only the sink path is included.
    """

    version: str
    python: str
    platform: str
    checks: list[DoctorCheck]
    settings: dict[str, object]
    config_path: str
    config_exists: bool
    loaded_paths: list[str]
    token_present: bool
    token_source: str | None
    env: dict[str, str]
    log_file: str

    @override
    def table_title(self) -> str | None:
        return "Diagnostics bundle"

    @override
    def table_columns(self) -> list[str]:
        return ["Field", "Value"]

    @override
    def table_rows(self) -> list[list[str]]:
        ok = sum(1 for c in self.checks if c.status == "ok")
        warn = sum(1 for c in self.checks if c.status == "warn")
        error = sum(1 for c in self.checks if c.status == "error")
        token = f"present ({self.token_source})" if self.token_present else "absent"
        return [
            ["version", self.version],
            ["python", self.python],
            ["platform", self.platform],
            ["checks", f"{ok} ok / {warn} warn / {error} error"],
            ["config path", self.config_path],
            ["config exists", "yes" if self.config_exists else "no"],
            ["loaded configs", ", ".join(self.loaded_paths) or "-"],
            ["token", token],
            ["env", ", ".join(sorted(self.env)) or "-"],
            ["log file", self.log_file],
        ]


type RemoteProtocol = Literal["https", "ssh", "local", "none"]


class GitExecResult(BaseModel):
    """Low-level result of one git subprocess invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class SyncOutcome(StrEnum):
    """Outcome for a single repository processed by ``repo sync``."""

    UPDATED = "updated"
    UPDATED_OFF_BRANCH = "updated_off_branch"
    WOULD_UPDATE = "would_update"
    UP_TO_DATE = "up_to_date"
    AHEAD_LOCAL = "ahead_local"
    DIVERGED = "diverged"
    BLOCKED_DIRTY = "blocked_dirty"
    STASH_CONFLICT = "stash_conflict"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    ERROR = "error"


#: Outcomes that cause ``repo sync`` to return a non-zero exit code.
FAILURE_OUTCOMES: frozenset[SyncOutcome] = frozenset(
    {
        SyncOutcome.BLOCKED_DIRTY,
        SyncOutcome.STASH_CONFLICT,
        SyncOutcome.TIMEOUT,
        SyncOutcome.ERROR,
    }
)


class DiscoveredRepo(BaseModel):
    """One repository discovered under a configured root."""

    path: str
    name: str
    root: str
    relative_path: str
    is_worktree: bool = False
    common_dir: str = ""
    remote_url: str | None = None
    remote_protocol: RemoteProtocol = "none"
    tags: list[str] = Field(default_factory=list)


class RepoListResult(CLIResult):
    """Discovery listing for ``repo list``."""

    repos: list[DiscoveredRepo]
    skipped_worktrees: list[str] = Field(default_factory=list)
    roots_scanned: list[str] = Field(default_factory=list)

    @override
    def table_title(self) -> str | None:
        return f"Repositories ({len(self.repos)})"

    @override
    def table_columns(self) -> list[str]:
        return ["Repository", "Root", "Kind", "Protocol", "Tags"]

    @override
    def table_rows(self) -> list[list[str]]:
        return [
            [
                r.relative_path,
                r.root,
                "worktree" if r.is_worktree else "clone",
                r.remote_protocol,
                ", ".join(r.tags) or "-",
            ]
            for r in self.repos
        ]

    @override
    def human_note(self) -> str | None:
        if not self.repos:
            return "No repositories found."
        return None


class RepoSyncItem(BaseModel):
    """Sync result for a single repository."""

    name: str
    path: str
    root: str
    relative_path: str
    branch: str
    default_branch: str | None = None
    outcome: SyncOutcome
    ahead: int = 0
    behind: int = 0
    old_sha: str | None = None
    new_sha: str | None = None
    commits_pulled: int = 0
    recent_commits: list[str] = Field(default_factory=list)
    stashed: bool = False
    tracked_dirty: int = 0
    untracked: int = 0
    detail: str = ""
    duration_ms: int = 0


_OUTCOME_STYLE: dict[SyncOutcome, tuple[str, str]] = {
    SyncOutcome.UPDATED: ("green", "updated"),
    SyncOutcome.UPDATED_OFF_BRANCH: ("green", "updated (ref)"),
    SyncOutcome.WOULD_UPDATE: ("cyan", "would update"),
    SyncOutcome.UP_TO_DATE: ("dim", "up to date"),
    SyncOutcome.AHEAD_LOCAL: ("yellow", "ahead"),
    SyncOutcome.DIVERGED: ("yellow", "diverged"),
    SyncOutcome.BLOCKED_DIRTY: ("red", "blocked (dirty)"),
    SyncOutcome.STASH_CONFLICT: ("red bold", "stash conflict"),
    SyncOutcome.SKIPPED: ("yellow", "skipped"),
    SyncOutcome.TIMEOUT: ("red", "timeout"),
    SyncOutcome.ERROR: ("red", "error"),
}


class SyncReport(CLIResult):
    """Aggregated result for ``repo sync``."""

    items: list[RepoSyncItem]
    skipped_worktrees: int = 0
    dry_run: bool = False
    only_changed: bool = False
    cancelled: bool = False

    def has_failures(self) -> bool:
        """True when any repository errored, timed out, or was blocked."""
        return any(item.outcome in FAILURE_OUTCOMES for item in self.items)

    def counts(self) -> dict[str, int]:
        """Return per-outcome item counts."""
        result: dict[str, int] = {}
        for item in self.items:
            key = item.outcome.value
            result[key] = result.get(key, 0) + 1
        return result

    def _visible_items(self) -> list[RepoSyncItem]:
        if not self.only_changed:
            return self.items
        return [i for i in self.items if i.outcome is not SyncOutcome.UP_TO_DATE]

    @staticmethod
    def _format_detail(item: RepoSyncItem) -> str:
        parts: list[str] = []
        if item.old_sha and item.new_sha and item.old_sha != item.new_sha:
            delta = f"{item.old_sha}..{item.new_sha}"
            if item.commits_pulled:
                delta += f" (+{item.commits_pulled})"
            parts.append(delta)
        elif item.ahead or item.behind:
            parts.append(f"+{item.ahead}/-{item.behind}")
        if item.stashed:
            parts.append("stashed+restored")
        if item.detail:
            parts.append(item.detail)
        return "; ".join(parts) or "-"

    @override
    def table_title(self) -> str | None:
        prefix = "Sync plan (dry-run)" if self.dry_run else "Sync summary"
        total = len(self.items)
        up_to_date = sum(1 for i in self.items if i.outcome is SyncOutcome.UP_TO_DATE)
        if self.only_changed and up_to_date:
            return f"{prefix} ({total} total, {up_to_date} up to date hidden)"
        return f"{prefix} ({total} repositories)"

    @override
    def table_columns(self) -> list[str]:
        return ["Repository", "Branch", "Default", "Outcome", "Detail"]

    @override
    def table_rows(self) -> list[list[str]]:
        return [
            [
                item.relative_path,
                item.branch,
                item.default_branch or "-",
                _OUTCOME_STYLE[item.outcome][1],
                self._format_detail(item),
            ]
            for item in self._visible_items()
        ]

    @override
    def table_rows_rich(self) -> list[list[str]]:
        rows: list[list[str]] = []
        for item in self._visible_items():
            style, label = _OUTCOME_STYLE[item.outcome]
            rows.append(
                [
                    item.relative_path,
                    item.branch,
                    item.default_branch or "-",
                    f"[{style}]{label}[/{style}]",
                    self._format_detail(item),
                ]
            )
        return rows

    @override
    def human_note(self) -> str | None:
        if not self.items:
            return "No repositories found."
        if not self._visible_items():
            return f"All {len(self.items)} repositories are up to date."
        return None


class StatusSyncState(StrEnum):
    """Remote synchronization state for a single repository in ``repo status``."""

    CLEAN = "clean"
    AHEAD = "ahead"
    BEHIND = "behind"
    DIVERGED = "diverged"
    LOCAL_ONLY = "local_only"
    NO_TRACKING = "no_tracking"
    ERROR = "error"


class StatusFilter(StrEnum):
    """Filter predicate choices for ``repo status --filter``."""

    ALL = "all"
    DIRTY = "dirty"
    CLEAN = "clean"
    AHEAD = "ahead"
    BEHIND = "behind"
    DIVERGED = "diverged"
    LOCAL = "local"
    ISSUES = "issues"


class RepoStatusItem(BaseModel):
    """Read-only status for a single repository (``repo status``)."""

    name: str
    path: str
    root: str
    relative_path: str
    branch: str
    default_branch: str | None = None
    tracked_dirty: int = 0
    untracked: int = 0
    ahead: int = 0
    behind: int = 0
    remote_protocol: RemoteProtocol = "none"
    detail: str = ""
    sync_state: StatusSyncState = StatusSyncState.CLEAN
    has_issues: bool = False

    @property
    def is_clean(self) -> bool:
        return self.tracked_dirty == 0 and self.untracked == 0


def matches_status_filter(item: RepoStatusItem, filt: StatusFilter) -> bool:
    """Return True when ``item`` satisfies ``filt``."""
    match filt:
        case StatusFilter.ALL:
            return True
        case StatusFilter.DIRTY:
            return not item.is_clean
        case StatusFilter.CLEAN:
            return item.is_clean and item.sync_state is StatusSyncState.CLEAN
        case StatusFilter.AHEAD:
            return item.ahead > 0
        case StatusFilter.BEHIND:
            return item.behind > 0
        case StatusFilter.DIVERGED:
            return item.sync_state is StatusSyncState.DIVERGED
        case StatusFilter.LOCAL:
            return item.sync_state is StatusSyncState.LOCAL_ONLY
        case StatusFilter.ISSUES:
            return item.has_issues
        case _:  # pragma: no cover — exhaustive over StatusFilter
            assert_never(filt)


def _format_sync_plain(item: RepoStatusItem) -> str:
    match item.sync_state:
        case StatusSyncState.CLEAN:
            return "clean"
        case StatusSyncState.AHEAD:
            return f"ahead +{item.ahead}"
        case StatusSyncState.BEHIND:
            return f"behind -{item.behind}"
        case StatusSyncState.DIVERGED:
            return f"diverged +{item.ahead}/-{item.behind}"
        case StatusSyncState.LOCAL_ONLY:
            return "local"
        case StatusSyncState.NO_TRACKING:
            return "-"
        case StatusSyncState.ERROR:
            return "error"
        case _:  # pragma: no cover — exhaustive over StatusSyncState
            assert_never(item.sync_state)


def _format_sync_rich(item: RepoStatusItem) -> str:
    match item.sync_state:
        case StatusSyncState.CLEAN:
            return "[green]✓[/green]"
        case StatusSyncState.AHEAD:
            return f"[yellow]↑{item.ahead}[/yellow]"
        case StatusSyncState.BEHIND:
            return f"[red]↓{item.behind}[/red]"
        case StatusSyncState.DIVERGED:
            return f"[red]↕+{item.ahead}/-{item.behind}[/red]"
        case StatusSyncState.LOCAL_ONLY:
            return "[blue]L[/blue]"
        case StatusSyncState.NO_TRACKING:
            return "[dim]-[/dim]"
        case StatusSyncState.ERROR:
            return "[red]✗[/red]"
        case _:  # pragma: no cover — exhaustive over StatusSyncState
            assert_never(item.sync_state)


def _format_sync_detail_rich(item: RepoStatusItem) -> str:
    match item.sync_state:
        case StatusSyncState.CLEAN:
            return "[green]✓ In sync with remote[/green]"
        case StatusSyncState.AHEAD:
            return f"[yellow]↑ {item.ahead} commit(s) ahead of remote[/yellow]"
        case StatusSyncState.BEHIND:
            return f"[red]↓ {item.behind} commit(s) behind remote[/red]"
        case StatusSyncState.DIVERGED:
            return f"[red]↕ Diverged (+{item.ahead} ahead, -{item.behind} behind)[/red]"
        case StatusSyncState.LOCAL_ONLY:
            return "[blue]L Local only (no remotes configured)[/blue]"
        case StatusSyncState.NO_TRACKING:
            return "[dim]- No remote tracking branch[/dim]"
        case StatusSyncState.ERROR:
            reason = item.detail or "fetch failed"
            return f"[red]✗ Error: {reason}[/red]"
        case _:  # pragma: no cover — exhaustive over StatusSyncState
            assert_never(item.sync_state)


def _format_sync_detail_plain(item: RepoStatusItem) -> str:
    match item.sync_state:
        case StatusSyncState.CLEAN:
            return "In sync with remote"
        case StatusSyncState.AHEAD:
            return f"Ahead (+{item.ahead} commit(s))"
        case StatusSyncState.BEHIND:
            return f"Behind (-{item.behind} commit(s))"
        case StatusSyncState.DIVERGED:
            return f"Diverged (+{item.ahead} ahead, -{item.behind} behind)"
        case StatusSyncState.LOCAL_ONLY:
            return "Local only (no remotes configured)"
        case StatusSyncState.NO_TRACKING:
            return "No remote tracking branch"
        case StatusSyncState.ERROR:
            reason = item.detail or "fetch failed"
            return f"Error: {reason}"
        case _:  # pragma: no cover — exhaustive over StatusSyncState
            assert_never(item.sync_state)


class RepoStatusReport(CLIResult):
    """Aggregated status for ``repo status``."""

    items: list[RepoStatusItem]
    fetched: bool = False
    skipped_worktrees: int = 0
    detailed: bool = Field(default=False, exclude=True)
    filters: list[StatusFilter] = Field(default_factory=list, exclude=True)

    def _active_filters(self) -> list[StatusFilter]:
        if not self.filters or StatusFilter.ALL in self.filters:
            return []
        return list(dict.fromkeys(self.filters))

    def summary_counts(self) -> dict[str, int]:
        """Return summary counts over reported repositories."""
        return {
            "total": len(self.items),
            "dirty": sum(1 for i in self.items if not i.is_clean),
            "ahead": sum(
                1 for i in self.items if i.sync_state is StatusSyncState.AHEAD
            ),
            "behind": sum(
                1 for i in self.items if i.sync_state is StatusSyncState.BEHIND
            ),
            "diverged": sum(
                1 for i in self.items if i.sync_state is StatusSyncState.DIVERGED
            ),
            "local": sum(
                1 for i in self.items if i.sync_state is StatusSyncState.LOCAL_ONLY
            ),
            "errors": sum(
                1 for i in self.items if i.sync_state is StatusSyncState.ERROR
            ),
        }

    @override
    def table_title(self) -> str | None:
        mode = "fetched" if self.fetched else "local"
        active = self._active_filters()
        if active:
            filter_label = ", ".join(f.value for f in active)
            return (
                f"Repository status ({len(self.items)}, {mode}, filter: {filter_label})"
            )
        return f"Repository status ({len(self.items)}, {mode})"

    @override
    def table_columns(self) -> list[str]:
        return [
            "Repository",
            "Branch",
            "Sync",
            "Dirty",
            "Untracked",
            "Ahead",
            "Behind",
            "Protocol",
        ]

    @override
    def table_rows(self) -> list[list[str]]:
        return [
            [
                i.relative_path,
                i.branch,
                _format_sync_plain(i),
                str(i.tracked_dirty) if i.tracked_dirty else "-",
                str(i.untracked) if i.untracked else "-",
                str(i.ahead) if i.ahead else "-",
                str(i.behind) if i.behind else "-",
                i.remote_protocol,
            ]
            for i in self.items
        ]

    @override
    def table_rows_rich(self) -> list[list[str]]:
        return [
            [
                i.relative_path,
                f"[cyan]{i.branch}[/cyan]",
                _format_sync_rich(i),
                f"[red]{i.tracked_dirty}[/red]" if i.tracked_dirty else "[dim]-[/dim]",
                f"[yellow]{i.untracked}[/yellow]" if i.untracked else "[dim]-[/dim]",
                f"[yellow]{i.ahead}[/yellow]" if i.ahead else "[dim]-[/dim]",
                f"[red]{i.behind}[/red]" if i.behind else "[dim]-[/dim]",
                i.remote_protocol,
            ]
            for i in self.items
        ]

    @override
    def render_text(self) -> str | None:
        if not self.detailed or not self.items:
            return None
        blocks: list[str] = []
        title = self.table_title()
        if title:
            blocks.append(f"[bold]{title}[/bold]")
        for item in self.items:
            default_label = item.default_branch or "-"
            lines = [
                f"[bold cyan]{item.relative_path}[/bold cyan]",
                f"  Path: {item.path}",
                f"  Branch: [cyan]{item.branch}[/cyan] (default: {default_label})",
            ]
            if item.is_clean:
                lines.append("  Working tree: [green]✓ Clean[/green]")
            else:
                wt_parts: list[str] = []
                if item.tracked_dirty:
                    wt_parts.append(f"[red]✗ {item.tracked_dirty} dirty tracked[/red]")
                if item.untracked:
                    wt_parts.append(f"[yellow]{item.untracked} untracked[/yellow]")
                lines.append(f"  Working tree: {', '.join(wt_parts)}")
            lines.append(f"  Sync: {_format_sync_detail_rich(item)}")
            lines.append(f"  Protocol: {item.remote_protocol}")
            if item.detail and item.sync_state is not StatusSyncState.ERROR:
                lines.append(f"  Detail: {item.detail}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    @override
    def render_markdown(self) -> str | None:
        if not self.detailed or not self.items:
            return None
        sections: list[str] = []
        title = self.table_title()
        if title:
            sections.append(f"## {title}")
        for item in self.items:
            default_label = item.default_branch or "-"
            if item.is_clean:
                wt_str = "Clean"
            else:
                wt_parts: list[str] = []
                if item.tracked_dirty:
                    wt_parts.append(f"{item.tracked_dirty} dirty tracked")
                if item.untracked:
                    wt_parts.append(f"{item.untracked} untracked")
                wt_str = ", ".join(wt_parts)
            lines = [
                f"### {item.relative_path}",
                "",
                f"- **Path:** `{item.path}`",
                f"- **Branch:** `{item.branch}` (default: `{default_label}`)",
                f"- **Working tree:** {wt_str}",
                f"- **Sync:** {_format_sync_detail_plain(item)}",
                f"- **Protocol:** `{item.remote_protocol}`",
            ]
            if item.detail and item.sync_state is not StatusSyncState.ERROR:
                lines.append(f"- **Detail:** {item.detail}")
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

    @override
    def human_note(self) -> str | None:
        if not self.items:
            active = self._active_filters()
            if active:
                filter_label = ", ".join(f.value for f in active)
                return f"No repositories match filter: {filter_label}."
            return "No repositories found."
        return None


class ConfigViewEntry(BaseModel):
    """One resolved key/value entry for ``config view``."""

    key: str
    value: str
    origin: str


class ConfigView(CLIResult):
    """Effective configuration listing for ``config view``."""

    entries: list[ConfigViewEntry]
    show_origin: bool = False

    @override
    def table_title(self) -> str | None:
        return "Effective configuration"

    @override
    def table_columns(self) -> list[str]:
        return ["Key", "Value", "Origin"] if self.show_origin else ["Key", "Value"]

    @override
    def table_rows(self) -> list[list[str]]:
        if self.show_origin:
            return [[e.key, e.value, e.origin] for e in self.entries]
        return [[e.key, e.value] for e in self.entries]
