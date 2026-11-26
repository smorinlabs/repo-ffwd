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

"""Typed configuration schema for the TOML config file.

Config is organized into tables by concern — ``[sync]``, ``[defaults]``,
``[output]``, ``[logging]``, and ``[[roots]]`` — with ``snake_case`` keys.
This module is the single source of truth for that schema: the loader validates
files against it, and ``config set``/``config get`` operate on **dotted keys**
(e.g. ``sync.concurrency``, ``logging.level``) resolved against it.

Secrets are deliberately *not* part of this schema — credentials stay in git's
credential helper / SSH agent (or ``--token`` / ``$REPO_UPDATER_TOKEN``).
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from repo_updater.core.errors import ConfigError

_Level = Literal["debug", "info", "warning", "error", "critical"]

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".*",
    "_archive",
    "archive",
    "node_modules",
    ".venv",
)


class SyncSettings(BaseModel):
    """The ``[sync]`` table controlling parallel fetch/update behavior."""

    model_config = {"extra": "ignore"}

    concurrency: int = Field(default=6, ge=1, le=64)
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    retries: int = Field(default=1, ge=0, le=5)
    prune: bool = True
    autostash: bool = False
    include_worktrees: bool = False
    remote: str = "origin"


class DefaultsSettings(BaseModel):
    """The ``[defaults]`` table for repository discovery defaults."""

    model_config = {"extra": "ignore"}

    depth: int = Field(default=2, ge=0, le=10)
    exclude: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDES))


class RootConfig(BaseModel):
    """One ``[[roots]]`` entry defining a directory tree to scan."""

    model_config = {"extra": "ignore"}

    path: str = Field(min_length=1)
    depth: int | None = Field(default=None, ge=0, le=10)
    exclude: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class OutputSettings(BaseModel):
    """The ``[output]`` table."""

    model_config = {"extra": "ignore"}

    format: Literal["text", "json", "markdown"] = "text"
    color: Literal["auto", "always", "never"] = "auto"
    only_changed: bool = True


class LoggingSettings(BaseModel):
    """The ``[logging]`` table."""

    model_config = {"extra": "ignore"}

    level: _Level = "warning"
    file: str = ""
    file_level: _Level = "debug"
    format: Literal["text", "json"] = "text"


class Settings(BaseModel):
    """Resolved, validated config (defaults applied for anything unset)."""

    model_config = {"extra": "ignore"}

    sync: SyncSettings = Field(default_factory=SyncSettings)
    defaults: DefaultsSettings = Field(default_factory=DefaultsSettings)
    roots: list[RootConfig] = Field(default_factory=list)
    output: OutputSettings = Field(default_factory=OutputSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


#: section name -> submodel, the tables `config set`/`get` accept.
_SECTIONS: dict[str, type[BaseModel]] = {
    "sync": SyncSettings,
    "defaults": DefaultsSettings,
    "output": OutputSettings,
    "logging": LoggingSettings,
}

#: Scalar config values that `config set` can write directly.
type ScalarConfigValue = str | int | bool


def _is_scalar_field(model: type[BaseModel], key: str) -> bool:
    """True when a model field is a scalar writable via ``config set``."""
    annotation = model.model_fields[key].annotation
    return (
        annotation is not list and getattr(annotation, "__origin__", None) is not list
    )


def writable_keys() -> list[str]:
    """All settable scalar dotted keys, e.g. ``["logging.level", "sync.concurrency", …]``."""
    return sorted(
        f"{section}.{name}"
        for section, model in _SECTIONS.items()
        for name in model.model_fields
        if _is_scalar_field(model, name)
    )


def parse_key(dotted: str) -> tuple[str, str]:
    """Split ``"logging.level"`` into ``("logging", "level")``, validating both.

    Raises :class:`ConfigError` for an unknown section or key (never a secret).
    """
    parts = dotted.split(".")
    if len(parts) != 2 or parts[0] not in _SECTIONS:
        raise ConfigError(
            f"unknown config key {dotted!r}. "
            f"Settable keys: {', '.join(writable_keys())}"
        )
    section, key = parts
    model = _SECTIONS[section]
    if key not in model.model_fields or not _is_scalar_field(model, key):
        raise ConfigError(
            f"unknown config key {dotted!r}. "
            f"Settable keys: {', '.join(writable_keys())}"
        )
    return section, key


def coerce_value(section: str, key: str, raw: str) -> ScalarConfigValue:
    """Validate + coerce a raw string against the field's type/allowed values."""
    model = _SECTIONS[section]
    annotation = model.model_fields[key].annotation
    candidate: Any = raw
    if annotation is bool:
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            candidate = True
        elif lowered in ("false", "0", "no", "off"):
            candidate = False
        else:
            raise ConfigError(
                f"invalid value for {section}.{key}: {raw!r} (allowed: true, false)"
            )
    try:
        validated = model.model_validate({key: candidate})
    except ValidationError as exc:
        allowed = _allowed_hint(model, key)
        hint = f" (allowed: {allowed})" if allowed else ""
        raise ConfigError(f"invalid value for {section}.{key}: {raw!r}{hint}") from exc
    value = getattr(validated, key)
    if not isinstance(
        value, (str, int, bool)
    ):  # pragma: no cover — guarded by _is_scalar_field
        raise ConfigError(f"unsupported value type for {section}.{key}: {raw!r}")
    return value


def allowed_values(section: str, key: str) -> tuple[str, ...] | None:
    """Allowed string literals for a settable key, or None if unconstrained.

    Used for error messages and interactive prompts (``config init``).
    """
    annotation = _SECTIONS[section].model_fields[key].annotation
    if annotation is bool:
        return ("true", "false")
    choices = getattr(annotation, "__args__", None)
    if choices and all(isinstance(c, str) for c in choices):
        return tuple(choices)
    return None


def _allowed_hint(model: type[BaseModel], key: str) -> str | None:
    """Render the allowed values for a Literal/bool field, for error messages."""
    annotation = model.model_fields[key].annotation
    if annotation is bool:
        return "true, false"
    choices = getattr(annotation, "__args__", None)
    if choices and all(isinstance(c, str) for c in choices):
        return ", ".join(choices)
    return None


def settings_from_layers(
    layers: list[dict[str, Any]],
) -> tuple[Settings, list[str]]:
    """Merge config dicts (lowest precedence first) and validate into Settings.

    Tables merge recursively; arrays (including ``[[roots]]`` and ``exclude``)
    replace from nearer scope. Invalid values are **dropped with a warning**
    rather than raised so config never bricks the CLI. Returns
    ``(settings, warnings)``.
    """
    merged: dict[str, Any] = {}
    warnings: list[str] = []

    for layer in layers:
        for section in _SECTIONS:
            table = layer.get(section)
            if isinstance(table, dict):
                merged.setdefault(section, {}).update(table)
        if "roots" in layer:
            raw_roots = layer["roots"]
            if isinstance(raw_roots, list):
                valid_roots: list[dict[str, Any]] = []
                for idx, entry in enumerate(raw_roots):
                    if not isinstance(entry, dict):
                        warnings.append(
                            f"ignoring invalid config value roots[{idx}] = {entry!r}"
                        )
                        continue
                    try:
                        RootConfig.model_validate(entry)
                        valid_roots.append(entry)
                    except ValidationError:
                        warnings.append(
                            f"ignoring invalid config value roots[{idx}] = {entry!r}"
                        )
                merged["roots"] = valid_roots
            else:
                warnings.append(
                    f"ignoring invalid config value roots = {raw_roots!r} (expected list)"
                )

    try:
        return Settings.model_validate(merged), warnings
    except ValidationError as exc:
        for err in exc.errors():
            loc = err["loc"][:2]
            if len(loc) == 2 and loc[0] in _SECTIONS and loc[0] in merged:
                section, key = str(loc[0]), str(loc[1])
                bad = merged[section].pop(key, None)
                hint = (
                    _allowed_hint(_SECTIONS[section], key)
                    if (key in _SECTIONS[section].model_fields)
                    else None
                )
                suffix = f" (allowed: {hint})" if hint else ""
                warnings.append(
                    f"ignoring invalid config value {section}.{key} = {bad!r}{suffix}"
                )
    try:
        return Settings.model_validate(merged), warnings
    except ValidationError:  # pragma: no cover — defensive double-fault
        warnings.append("config could not be validated; using built-in defaults")
        return Settings(), warnings
