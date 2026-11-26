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

"""A click ``Group`` that supports hidden aliases and near-miss suggestions.

Used by the root group and every noun group, so ``repo-updater rpo sync`` and
``repo-updater repo snc`` answer with "Did you mean …?" instead of a bare
"No such command", while undocumented plural aliases (e.g. ``repos`` -> ``repo``)
resolve transparently without cluttering ``--help``.
"""

import difflib
from typing import Any, override

import click

#: ``get_close_matches`` cutoff — 0.6 is the difflib default; tight enough
#: that unrelated names don't surface, loose enough for 1-2 typo edits.
_CUTOFF = 0.6


class SuggestingGroup(click.Group):
    """``click.Group`` with undocumented command aliases and close-match suggestions."""

    def __init__(
        self,
        *args: Any,  # noqa: ANN401 — forwarded verbatim to click.Group
        aliases: dict[str, str] | None = None,
        **kwargs: Any,  # noqa: ANN401 — forwarded verbatim to click.Group
    ) -> None:
        super().__init__(*args, **kwargs)
        self._aliases: dict[str, str] = dict(aliases or {})

    @override
    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        canonical = self._aliases.get(cmd_name, cmd_name)
        return super().get_command(ctx, canonical)

    @override
    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            if not args:
                raise
            name = click.utils.make_str(args[0])
            matches = difflib.get_close_matches(
                name, self.list_commands(ctx), n=3, cutoff=_CUTOFF
            )
            if not matches:
                raise
            suggestion = " or ".join(f"'{m}'" for m in matches)
            raise click.UsageError(
                f"No such command '{name}'. Did you mean {suggestion}?", ctx
            ) from exc
