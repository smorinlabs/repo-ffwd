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

"""``repo-updater config`` — inspect and edit configuration (no network required).

``config set``/``get`` operate on **dotted keys** into the config tables, e.g.
``sync.concurrency``, ``defaults.depth``, ``output.color``, or ``logging.level``.
``config init`` runs a guided interactive setup for root directories and sync
defaults, and ``config view`` displays the effective merged configuration.
"""

from pathlib import Path
from typing import Any

import click

from repo_updater.cli.context import AppContext
from repo_updater.cli.groups import SuggestingGroup
from repo_updater.cli.options import confirm, global_options, mutation_options
from repo_updater.core.config import (
    get_default_config_path,
    read_config_for_write,
    write_config_data,
)
from repo_updater.core.errors import ConfigError, ExitCode
from repo_updater.core.models import (
    ConfigPath,
    ConfigValue,
    ConfigView,
    ConfigViewEntry,
)
from repo_updater.core.settings import (
    DEFAULT_EXCLUDES,
    allowed_values,
    coerce_value,
    parse_key,
    writable_keys,
)

#: Keys `config get` can resolve: every settable scalar key plus the read-only token.
_GETTABLE_KEYS = sorted([*writable_keys(), "token"])

#: Keys ``config init`` walks through, in prompt order.
_INIT_KEYS = [
    "defaults.depth",
    "sync.concurrency",
    "sync.timeout_seconds",
    "sync.autostash",
    "output.format",
    "output.color",
    "logging.level",
]


def _parse_roots_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def run_guided_config_init(
    app: AppContext,
    *,
    dry_run: bool = False,
    assume_yes: bool = False,
    root_paths: tuple[str, ...] = (),
) -> Path:
    """Create or update the config file via guided prompts (or ``--yes`` defaults).

    Shared by ``config init`` and the first-run setup prompt in ``repo`` commands.
    Returns the target config path.
    """
    path = app.config.config_path or get_default_config_path()
    settings = app.config.settings

    existing_roots = [r.path for r in settings.roots]
    default_roots_str = ", ".join(root_paths or existing_roots or ["~/projects"])

    current: dict[str, str] = {}
    for dotted in _INIT_KEYS:
        section, name = parse_key(dotted)
        val = getattr(getattr(settings, section), name)
        current[dotted] = str(val).lower() if isinstance(val, bool) else str(val)

    if assume_yes:
        chosen_roots = list(root_paths) if root_paths else existing_roots
        chosen = current
    elif app.no_input:
        raise ConfigError(
            "config init cannot prompt (--no-input set)",
            hint="pass --yes to write the current defaults non-interactively",
        )
    else:
        raw_roots = click.prompt(
            "Root directories to scan (comma-separated)",
            default=default_roots_str,
            type=str,
            err=True,
        )
        chosen_roots = _parse_roots_csv(raw_roots)
        chosen = {}
        for dotted, default in current.items():
            section, name = parse_key(dotted)
            choices = allowed_values(section, name)
            chosen[dotted] = click.prompt(
                dotted,
                default=default,
                type=click.Choice(choices) if choices else str,
                err=True,
            )

    if dry_run:
        if chosen_roots:
            app.renderer.message(
                f"[dry-run] would set roots = {chosen_roots} in {path}"
            )
        for dotted, value in chosen.items():
            app.renderer.message(f"[dry-run] would set {dotted} = {value} in {path}")
        return path

    data = read_config_for_write(path)  # ConfigError if existing+corrupt
    for dotted, value in chosen.items():
        section, name = parse_key(dotted)
        table = data.get(section)
        if not isinstance(table, dict):
            table = {}
        table[name] = coerce_value(section, name, value)
        data[section] = table

    defaults_table = data.get("defaults")
    if not isinstance(defaults_table, dict):
        defaults_table = {}
    defaults_table.setdefault("exclude", list(DEFAULT_EXCLUDES))
    data["defaults"] = defaults_table

    if chosen_roots:
        existing_by_path: dict[str, dict[str, Any]] = {}
        raw_existing = data.get("roots")
        if isinstance(raw_existing, list):
            for entry in raw_existing:
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    existing_by_path[entry["path"]] = entry
        data["roots"] = [
            existing_by_path.get(root_path, {"path": root_path})
            for root_path in chosen_roots
        ]

    write_config_data(path, data)
    app.renderer.message(f"Wrote {path}")
    return path


@click.group(name="config", cls=SuggestingGroup)
def config_group() -> None:
    """Inspect and edit CLI configuration."""


@config_group.command(name="init")
@click.option(
    "--root",
    "roots",
    multiple=True,
    help="Root directory to include in initial configuration (repeatable).",
)
@mutation_options
@global_options
def config_init(
    app: AppContext, roots: tuple[str, ...], dry_run: bool, assume_yes: bool
) -> None:
    """Create or update the config file with guided prompts.

    Asks (on stderr, so stdout stays pipe-safe) for root directories and the
    most common sync settings, offering currently resolved values as defaults,
    then writes them to the TOML config file. Examples:

        repo-updater config init
        repo-updater config init --root ~/projects --yes
        repo-updater config init --dry-run
    """
    path = run_guided_config_init(
        app, dry_run=dry_run, assume_yes=assume_yes, root_paths=roots
    )
    app.renderer.render(ConfigPath(path=str(path), exists=path.exists()))


@config_group.command(name="view")
@click.option(
    "--show-origin",
    is_flag=True,
    help="Show whether each resolved setting came from config or defaults.",
)
@global_options
def config_view(app: AppContext, show_origin: bool) -> None:
    """Show the effective merged configuration.

    Examples:
        repo-updater config view
        repo-updater config view --show-origin --json
    """
    settings = app.config.settings
    entries: list[ConfigViewEntry] = []
    for dotted in writable_keys():
        section, name = parse_key(dotted)
        table = getattr(settings, section)
        val = getattr(table, name)
        rendered = str(val).lower() if isinstance(val, bool) else str(val)
        origin = "config" if name in table.model_fields_set else "default"
        entries.append(ConfigViewEntry(key=dotted, value=rendered, origin=origin))
    entries.append(
        ConfigViewEntry(
            key="defaults.exclude",
            value=", ".join(settings.defaults.exclude) or "-",
            origin=(
                "config"
                if "exclude" in settings.defaults.model_fields_set
                else "default"
            ),
        )
    )
    roots_val = ", ".join(r.path for r in settings.roots) or "(none)"
    entries.append(
        ConfigViewEntry(
            key="roots",
            value=roots_val,
            origin="config" if settings.roots else "default",
        )
    )
    app.renderer.render(ConfigView(entries=entries, show_origin=show_origin))


@config_group.command(name="path")
@global_options
def config_path(app: AppContext) -> None:
    """Show the config file path and whether it exists.

    Examples:
        repo-updater config path
        repo-updater config path --json
    """
    path = app.config.config_path or get_default_config_path()
    app.renderer.render(ConfigPath(path=str(path), exists=path.exists()))


@config_group.command(name="get")
@click.argument("key", type=click.Choice(_GETTABLE_KEYS))
@global_options
def config_get(app: AppContext, key: str) -> None:
    """Show a resolved config value and where it came from.

    Examples:
        repo-updater config get sync.concurrency
        repo-updater config get output.color --json
    """
    cfg = app.config
    if key == "token":
        value = _mask(cfg.token) if cfg.token else None
        app.renderer.render(ConfigValue(key=key, value=value, source=cfg.source))
        return
    section, name = parse_key(key)
    table = getattr(cfg.settings, section)
    resolved = getattr(table, name)
    rendered = str(resolved).lower() if isinstance(resolved, bool) else str(resolved)
    source = "config" if name in table.model_fields_set else "default"
    app.renderer.render(ConfigValue(key=key, value=rendered, source=source))


@config_group.command(name="set")
@click.argument("key", type=click.Choice(writable_keys()))
@click.argument("value")
@mutation_options
@global_options
def config_set(
    app: AppContext, key: str, value: str, dry_run: bool, assume_yes: bool
) -> None:
    """Write a non-secret config value to the TOML config file.

    KEY is a dotted path into the config tables. Examples:
        repo-updater config set sync.concurrency 8
        repo-updater config set sync.autostash true
        repo-updater config set output.color always --yes
    """
    path = app.config.config_path or get_default_config_path()
    section, name = parse_key(key)
    coerced = coerce_value(section, name, value)  # validates up front
    rendered = str(coerced).lower() if isinstance(coerced, bool) else str(coerced)

    if dry_run:
        app.renderer.message(f"[dry-run] would set {key} = {rendered} in {path}")
        app.renderer.render(ConfigValue(key=key, value=rendered, source="dry-run"))
        return

    data = read_config_for_write(path)  # ConfigError if existing+corrupt
    table = data.get(section)
    if not isinstance(table, dict):
        table = {}
    existing = table.get(name)
    if (
        existing is not None
        and existing != coerced
        and not confirm(
            app,
            f"Overwrite {key} ({existing} → {coerced}) in {path}?",
            assume_yes=assume_yes,
        )
    ):
        app.renderer.message("Aborted.")
        raise SystemExit(int(ExitCode.INTERRUPT))

    table[name] = coerced
    data[section] = table
    write_config_data(path, data)
    app.renderer.message(f"Set {key} = {rendered} in {path}")
    app.renderer.render(ConfigValue(key=key, value=rendered, source="file"))


def _mask(secret: str) -> str:
    """Mask a secret, revealing only the last 4 characters."""
    if len(secret) <= 4:
        return "****"
    return "****" + secret[-4:]
