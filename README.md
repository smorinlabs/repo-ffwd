# Repo Ffwd

A safe, parallel, config-driven CLI and agent skill for keeping local Git repositories updated to their remote default branch.

## Features

- **Parallel & bounded:** Fetches repositories concurrently (`--concurrency`) with per-repository timeouts (`--timeout`) and transient network retries.
- **Non-interactive by construction:** Runs every `git` subprocess with closed stdin, `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, and non-interactive SSH (`BatchMode=yes` injected only when neither `GIT_SSH_COMMAND`/`GIT_SSH` nor `core.sshCommand` is configured).
- **Depth-bounded, zero double-scanning:** Walks configured `[[roots]]` up to `depth`, stops descending at `.git` boundaries, skips linked worktrees by default, honors exclude globs and `.repoignore`, and deduplicates overlapping roots and symlinks via realpath and `--git-common-dir`.
- **Smart conditional AutoStash (`--autostash`, default `false`):** Never touches or stashes repositories that are already up to date (`behind == 0`), ahead, or diverged. Only stashes when behind *and* dirty files block fast-forward, then immediately restores (`git stash pop`), preserving the stash on conflict.
- **Worktree-safe off-branch updates:** Updates local `<default>` refs via `git fetch <remote> <default>:<default>` without switching branches, skipping ref updates whenever `<default>` is checked out in a linked worktree.
- **Agent- & script-ready:** Noun-verb CLI conforming to CLI Design Standard v1.4.15 (`text`, `--json`, `-o markdown`), deterministic ordering, `--dry-run`, `doctor`, and a bundled skill plugin for Claude Code, Codex, and Antigravity.

## Installation

```bash
# Run on demand with uvx
uvx repo-ffwd --help

# Or install persistently
uv tool install repo-ffwd
# pip install repo-ffwd
```

## Quick Start

```bash
# 1. Guided initial setup (creates your XDG TOML config)
repo-ffwd config init

# 2. Preview discovered repositories (offline)
repo-ffwd repo list

# 3. Preview updates without mutating anything
repo-ffwd repo sync --dry-run

# 4. Sync all configured repositories in parallel
repo-ffwd repo sync
```

If you run `repo-ffwd repo sync` interactively before creating a config file, it prompts to run guided setup. In scripts or CI (`--no-input`, piped stdin/stderr, or JSON output mode), it exits cleanly with an actionable hint unless `--root <path>` is passed.

## Configuration

Config lives in your XDG config directory (`repo-ffwd config path` prints the exact path):

```toml
[sync]
concurrency = 6
timeout_seconds = 60
retries = 1
prune = true
autostash = false
include_worktrees = false
remote = "origin"

[defaults]
depth = 2
exclude = [".*", "_archive", "archive", "node_modules", ".venv"]

[output]
format = "text"
color = "auto"
only_changed = true

[[roots]]
path = "~/projects"
depth = 2
exclude = ["vendor/*"]
tags = ["work"]
```

Inspect and edit settings without touching the file directly:

```bash
repo-ffwd config view --show-origin
repo-ffwd config set sync.concurrency 8
repo-ffwd doctor
```

## Commands

- `repo-ffwd repo sync` — Fetch and fast-forward default branches in parallel (`--root`, `--max-depth`, `--concurrency`, `--timeout`, `--autostash`, `--dry-run`, `--all`, `--tag`, `--fail-fast`)
- `repo-ffwd repo list` — Offline discovery preview
- `repo-ffwd repo status` — Read-only branch, sync state, dirty, and ahead/behind status (`--fetch`, repeatable `--filter`, `--detailed`, `--legend`)
- `repo-ffwd config init | view | path | get | set` — Configuration management
- `repo-ffwd doctor` — Check Python, `git`, config file, and configured roots
- `repo-ffwd completion bash|zsh|fish` — Shell completions

See [docs/cli-interface.md](docs/cli-interface.md) for the complete interface contract and [CONFORMANCE.md](CONFORMANCE.md) for the CLI Design Standard v1.4.15 conformance record.

## Bundled Agent Skill

This repository ships the `repo-ffwd` skill plugin under [`plugins/repo-ffwd/`](plugins/repo-ffwd/) with manifests for Claude Code, Codex, and Antigravity.

## Development

```bash
just setup   # install dev env and git hooks
just check   # lint, typecheck, boundaries, and full pytest suite
uv build     # build wheel and sdist
```
