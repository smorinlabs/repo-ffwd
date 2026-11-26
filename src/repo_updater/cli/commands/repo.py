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

"""``repo-updater repo`` — discover, inspect, and sync local Git repositories."""

import signal
import sys
from threading import Event
from types import FrameType

import click

from repo_updater.cli.commands.config import run_guided_config_init
from repo_updater.cli.context import AppContext
from repo_updater.cli.groups import SuggestingGroup
from repo_updater.cli.options import global_options
from repo_updater.cli.output import OutputMode
from repo_updater.composition import build_repo_sync_service
from repo_updater.core.config import load_config
from repo_updater.core.errors import ConfigError
from repo_updater.core.models import RepoSyncItem, SyncOutcome
from repo_updater.core.settings import RootConfig


def _stdin_is_tty() -> bool:
    isatty = getattr(sys.stdin, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except ValueError:
        return False


def _can_prompt_setup(app: AppContext) -> bool:
    """True only when interactive setup prompting is allowed (Decision #51).

    Checks the resolved output mode (covering ``--json``, ``-o json``,
    ``$REPO_UPDATER_OUTPUT=json``, and config), ``--no-input``, and TTY state.
    """
    return (
        not app.no_input
        and app.output_mode is not OutputMode.JSON
        and _stdin_is_tty()
        and app.renderer.err.is_terminal
    )


def _resolve_roots(
    app: AppContext,
    cli_roots: tuple[str, ...],
    *,
    max_depth: int | None,
) -> list[RootConfig]:
    """Resolve scan roots from CLI flags, config, or interactive first-run setup."""
    if cli_roots:
        return [RootConfig(path=p, depth=max_depth) for p in cli_roots]

    if app.config.settings.roots:
        if max_depth is None:
            return list(app.config.settings.roots)
        return [
            r.model_copy(update={"depth": max_depth}) for r in app.config.settings.roots
        ]

    if _can_prompt_setup(app):
        app.renderer.message("No configuration with repository roots found.")
        if click.confirm("Set up configuration now?", default=True, err=True):
            written = run_guided_config_init(app)
            reloaded = load_config(config_file=str(written), token_override=app.token)
            if reloaded.settings.roots:
                if max_depth is None:
                    return list(reloaded.settings.roots)
                return [
                    r.model_copy(update={"depth": max_depth})
                    for r in reloaded.settings.roots
                ]

    raise ConfigError(
        "No configuration file with roots found and no --root provided.",
        hint="Run 'repo-updater config init' or pass '--root <path>'.",
    )


@click.group(name="repo", cls=SuggestingGroup)
def repo_group() -> None:
    """Discover, inspect, and sync local Git repositories."""


@repo_group.command(name="sync")
@click.option(
    "--root",
    "roots",
    multiple=True,
    help="Directory root to scan (repeatable; overrides configured roots).",
)
@click.option(
    "--max-depth",
    type=click.IntRange(min=0, max=10),
    default=None,
    help="Maximum directory traversal depth below each root.",
)
@click.option(
    "--concurrency",
    type=click.IntRange(min=1, max=64),
    default=None,
    envvar="REPO_UPDATER_CONCURRENCY",
    help="Maximum number of repositories to fetch in parallel.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=click.IntRange(min=1, max=3600),
    default=None,
    envvar="REPO_UPDATER_TIMEOUT",
    help="Per-repository git fetch timeout in seconds.",
)
@click.option(
    "--retries",
    type=click.IntRange(min=0, max=5),
    default=None,
    help="Retry attempts for transient network errors.",
)
@click.option(
    "--autostash/--no-autostash",
    default=None,
    help="Stash and restore local changes only when behind and blocked by dirty files.",
)
@click.option(
    "--prune/--no-prune",
    default=None,
    help="Prune deleted remote-tracking branches during fetch.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Fetch and compute the update plan without modifying any branch or tree.",
)
@click.option(
    "--only-changed/--all",
    "only_changed",
    default=None,
    help="Hide already up-to-date repositories in text output (use --all to show every repo).",
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Limit sync to configured roots carrying at least one matching tag.",
)
@click.option(
    "--include-worktrees/--no-include-worktrees",
    default=None,
    help="Include linked Git worktrees (skipped by default).",
)
@click.option(
    "--fail-fast",
    is_flag=True,
    help="Stop scheduling remaining repositories after the first failure.",
)
@global_options
def repo_sync(
    app: AppContext,
    roots: tuple[str, ...],
    max_depth: int | None,
    concurrency: int | None,
    timeout_seconds: int | None,
    retries: int | None,
    autostash: bool | None,
    prune: bool | None,
    dry_run: bool,
    only_changed: bool | None,
    tags: tuple[str, ...],
    include_worktrees: bool | None,
    fail_fast: bool,
) -> None:
    """Fetch and fast-forward default branches across repositories in parallel.

    Examples:
        repo-updater repo sync
        repo-updater repo sync --root ~/projects --concurrency 8
        repo-updater repo sync --dry-run --all
        repo-updater repo sync --autostash --json --no-input
    """
    settings = app.config.settings
    resolved_roots = _resolve_roots(app, roots, max_depth=max_depth)

    eff_depth = max_depth if max_depth is not None else settings.defaults.depth
    eff_concurrency = (
        concurrency if concurrency is not None else settings.sync.concurrency
    )
    eff_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else settings.sync.timeout_seconds
    )
    eff_retries = retries if retries is not None else settings.sync.retries
    eff_autostash = autostash if autostash is not None else settings.sync.autostash
    eff_prune = prune if prune is not None else settings.sync.prune
    eff_worktrees = (
        include_worktrees
        if include_worktrees is not None
        else settings.sync.include_worktrees
    )
    eff_only_changed = (
        only_changed if only_changed is not None else settings.output.only_changed
    )

    cancel_event = Event()
    received_signal: list[int] = []

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        received_signal.append(signum)
        cancel_event.set()

    prev_sigint = signal.getsignal(signal.SIGINT)
    prev_sigterm = (
        signal.getsignal(signal.SIGTERM) if hasattr(signal, "SIGTERM") else None
    )
    try:
        signal.signal(signal.SIGINT, _handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handle_signal)

        def _on_progress(item: RepoSyncItem) -> None:
            if app.quiet or app.output_mode is OutputMode.JSON:
                return
            if item.outcome is not SyncOutcome.UP_TO_DATE or app.verbose > 0:
                app.renderer.message(
                    f"[dim]{item.relative_path}:[/dim] {item.outcome.value}"
                )

        svc = build_repo_sync_service()
        report = svc.sync_repos(
            resolved_roots,
            default_depth=eff_depth,
            global_excludes=settings.defaults.exclude,
            include_worktrees=eff_worktrees,
            tags=list(tags),
            preferred_remote=settings.sync.remote,
            concurrency=eff_concurrency,
            timeout_seconds=eff_timeout,
            retries=eff_retries,
            prune=eff_prune,
            autostash=eff_autostash,
            dry_run=dry_run,
            only_changed=eff_only_changed,
            fail_fast=fail_fast,
            cancel_event=cancel_event,
            on_progress=_on_progress,
        )
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
        if prev_sigterm is not None and hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, prev_sigterm)

    app.renderer.render(report)

    if received_signal:
        sig = received_signal[0]
        if hasattr(signal, "SIGTERM") and sig == signal.SIGTERM:
            raise SystemExit(143)
        raise SystemExit(130)

    if not app.quiet and app.output_mode is OutputMode.TEXT and report.items:
        counts = report.counts()
        summary_parts = [f"{k}={v}" for k, v in sorted(counts.items())]
        if report.skipped_worktrees:
            summary_parts.append(f"worktrees_skipped={report.skipped_worktrees}")
        app.renderer.message("Summary: " + ", ".join(summary_parts))

    if report.has_failures():
        raise SystemExit(1)


@repo_group.command(name="list")
@click.option(
    "--root",
    "roots",
    multiple=True,
    help="Directory root to scan (repeatable; overrides configured roots).",
)
@click.option(
    "--max-depth",
    type=click.IntRange(min=0, max=10),
    default=None,
    help="Maximum directory traversal depth below each root.",
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Limit listing to configured roots carrying at least one matching tag.",
)
@click.option(
    "--include-worktrees/--no-include-worktrees",
    default=None,
    help="Include linked Git worktrees (skipped by default).",
)
@global_options
def repo_list(
    app: AppContext,
    roots: tuple[str, ...],
    max_depth: int | None,
    tags: tuple[str, ...],
    include_worktrees: bool | None,
) -> None:
    """List discovered Git repositories without making network calls.

    Examples:
        repo-updater repo list
        repo-updater repo list --root ~/projects --max-depth 2 --json
    """
    settings = app.config.settings
    resolved_roots = _resolve_roots(app, roots, max_depth=max_depth)
    eff_depth = max_depth if max_depth is not None else settings.defaults.depth
    eff_worktrees = (
        include_worktrees
        if include_worktrees is not None
        else settings.sync.include_worktrees
    )

    svc = build_repo_sync_service()
    result = svc.list_repos(
        resolved_roots,
        default_depth=eff_depth,
        global_excludes=settings.defaults.exclude,
        include_worktrees=eff_worktrees,
        tags=list(tags),
        preferred_remote=settings.sync.remote,
    )
    app.renderer.render(result)


@repo_group.command(name="status")
@click.option(
    "--root",
    "roots",
    multiple=True,
    help="Directory root to scan (repeatable; overrides configured roots).",
)
@click.option(
    "--max-depth",
    type=click.IntRange(min=0, max=10),
    default=None,
    help="Maximum directory traversal depth below each root.",
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Limit status to configured roots carrying at least one matching tag.",
)
@click.option(
    "--fetch/--no-fetch",
    default=False,
    help="Fetch remotes before computing ahead/behind counts (offline by default).",
)
@click.option(
    "--concurrency",
    type=click.IntRange(min=1, max=64),
    default=None,
    envvar="REPO_UPDATER_CONCURRENCY",
    help="Maximum number of parallel workers.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=click.IntRange(min=1, max=3600),
    default=None,
    envvar="REPO_UPDATER_TIMEOUT",
    help="Per-repository git fetch timeout in seconds (when --fetch is set).",
)
@click.option(
    "--include-worktrees/--no-include-worktrees",
    default=None,
    help="Include linked Git worktrees (skipped by default).",
)
@global_options
def repo_status(
    app: AppContext,
    roots: tuple[str, ...],
    max_depth: int | None,
    tags: tuple[str, ...],
    fetch: bool,
    concurrency: int | None,
    timeout_seconds: int | None,
    include_worktrees: bool | None,
) -> None:
    """Show read-only branch, dirty, and ahead/behind status across repositories.

    Examples:
        repo-updater repo status
        repo-updater repo status --fetch --json
    """
    settings = app.config.settings
    resolved_roots = _resolve_roots(app, roots, max_depth=max_depth)
    eff_depth = max_depth if max_depth is not None else settings.defaults.depth
    eff_concurrency = (
        concurrency if concurrency is not None else settings.sync.concurrency
    )
    eff_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else settings.sync.timeout_seconds
    )
    eff_worktrees = (
        include_worktrees
        if include_worktrees is not None
        else settings.sync.include_worktrees
    )

    svc = build_repo_sync_service()
    report = svc.status_repos(
        resolved_roots,
        default_depth=eff_depth,
        global_excludes=settings.defaults.exclude,
        include_worktrees=eff_worktrees,
        tags=list(tags),
        preferred_remote=settings.sync.remote,
        fetch=fetch,
        concurrency=eff_concurrency,
        timeout_seconds=eff_timeout,
    )
    app.renderer.render(report)
