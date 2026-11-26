# CLI Interface Specification — `repo-updater`

| | |
|---|---|
| **Binary** | `repo-updater` (with `repo_updater` alias) |
| **Standard** | CLI Design Standard **v1.4.15** |
| **Profile** | Standard (noun-verb, §1) |
| **Tier** | Publishable |
| **Status** | Active design contract |

This document is the normative CLI interface contract for `repo-updater`. Rule citations (`R#.#`, `Decision #`) refer to the CLI Design Standard v1.4.15.

---

## 1. Command Tree

Depth ≤ 2 subcommand path elements (`repo-updater <noun> <verb>`, Decision #11). Singular noun `repo` is canonical; plural `repos` is accepted as an undocumented alias (Decision #5). Bare `repo-updater` prints help on stdout and exits `0`; an incomplete group (`repo-updater repo`) prints usage on stderr and exits `2`; unknown commands suggest close matches (R7.9, Decision #34).

| Command | Verb class | Purpose |
|---|---|---|
| `repo-updater repo sync` | Domain verb (Decision #23) | Fetch and fast-forward default branches across discovered repositories in parallel |
| `repo-updater repo list` | Core verb (R2.1) | Discover and list repositories offline (no network), showing skipped worktrees and remote protocol |
| `repo-updater repo status` | Domain verb (Decision #23) | Report branch, clean/dirty, untracked, and ahead/behind sync state (offline by default; `--fetch` opt-in) |
| `repo-updater config init` | Setup verb (Decision #53) | Guided interactive setup writing the XDG TOML config file |
| `repo-updater config view` | Core verb (R2.1, Decision #40) | Display effective merged configuration (supports `--show-origin`) |
| `repo-updater config path` | Inspect verb | Print resolved config file path and existence |
| `repo-updater config get <key>` | Config read | Print one resolved dotted key and its origin |
| `repo-updater config set <key> <value>` | Config write (Decision #20) | Validate and write one dotted key to the user/target config file |
| `repo-updater doctor` | Diagnostics (R9.10) | Verify `git`, config, root paths, credential helper, and SSH-remote readiness (`--bundle` for redacted bug reports) |
| `repo-updater completion <shell>` | Ecosystem | Emit shell completion script for `bash`, `zsh`, or `fish` |

---

## 2. Standard Global Options (Appendix B)

Available on every command (R4.1–R4.4). Exact flag matching is enforced — prefix abbreviation is disabled (Decision #32).

| Long flag | Short | Type | Default | Env var | Notes |
|---|---|---|---|---|---|
| `--output` | `-o` | `text\|json\|markdown` | `text` | `REPO_UPDATER_OUTPUT` | Output format (Decision #4) |
| `--json` | — | bool | `false` | — | Shorthand for `--output json` |
| `--output-file` | — | path | — | — | Write results to path instead of stdout; long-only (Decision #25) |
| `--verbose` | `-v` | count | `0` | — | Repeatable (`-v` info, `-vv` debug) (Decision #3, #15) |
| `--quiet` | `-q` | bool | `false` | — | Suppress non-essential stderr output; wins ties over `-v` (Decision #15) |
| `--log-level` | — | level | `warning` | `REPO_UPDATER_LOG_LEVEL` | Explicit console log level |
| `--log-file` | — | optional path | off | `REPO_UPDATER_LOG_FILE` | Rotating file log (default: XDG state dir) |
| `--no-color` | — | bool | `false` | `NO_COLOR` | Disable ANSI colors |
| `--config` | — | path | discovered | `REPO_UPDATER_CONFIG` | Explicit TOML config file (overrides layered discovery) |
| `--no-input` | — | bool | `false` | — | Never prompt; fail cleanly instead (R8.5) |
| `--version` | `-V` | bool | — | — | Root option: prints `repo-updater <semver>` + Python/platform, exits `0` (Decision #3, #35) |
| `--help` | `-h` | bool | — | — | Help on stdout, exits `0` (R4.1) |

---

## 3. Command Flags

### `repo-updater repo sync`

| Long flag | Short | Type | Default | Repeatable | Negation | Notes |
|---|---|---|---|---|---|---|
| `--root` | — | path | from config | Yes | — | Override/supply scan root(s); bypasses missing-config error |
| `--max-depth` | — | int (≥ 0) | config (`2`) | No | — | Max directory depth below each root |
| `--concurrency` | — | int (≥ 1) | config (`6`) | No | — | Parallel worker count (Decision #40) |
| `--timeout` | — | int (≥ 1) | config (`60`) | No | — | Per-repo git fetch timeout in seconds (Decision #29) |
| `--retries` | — | int (≥ 0) | config (`1`) | No | — | Retries for transient DNS/connection errors only |
| `--autostash` | — | bool | config (`false`) | No | `--no-autostash` | Only stashes when `behind > 0` and dirty files block fast-forward, then pops |
| `--prune` | — | bool | config (`true`) | No | `--no-prune` | Pass `--prune` to `git fetch` |
| `--dry-run` | — | bool | `false` | No | — | Fetch and plan updates without modifying any branch or working tree (Decision #14) |
| `--only-changed` | — | bool | config (`true`) | No | `--all` | Text mode hides `up_to_date` repos unless `--all` is passed; JSON always returns all items |
| `--tag` | — | string | all | Yes | — | Limit to roots carrying at least one matching tag |
| `--include-worktrees` | — | bool | config (`false`) | No | `--no-include-worktrees` | Include linked git worktrees (skipped by default) |
| `--fail-fast` | — | bool | `false` | No | — | Stop scheduling new repos after the first error (Decision #37) |

### `repo-updater repo list`

| Long flag | Type | Default | Repeatable | Notes |
|---|---|---|---|---|
| `--root` | path | from config | Yes | One-off scan root(s) |
| `--max-depth` | int (≥ 0) | config (`2`) | No | Max directory depth |
| `--tag` | string | all | Yes | Filter roots by tag |
| `--include-worktrees` | bool | config (`false`) | No | Show linked worktrees alongside main clones |

### `repo-updater repo status`

| Long flag | Type | Default | Repeatable | Negation | Notes |
|---|---|---|---|---|---|
| `--root` | path | from config | Yes | — | One-off scan root(s) |
| `--max-depth` | int (≥ 0) | config (`2`) | No | — | Max directory depth |
| `--tag` | string | all | Yes | — | Filter roots by tag |
| `--fetch` | bool | `false` | No | `--no-fetch` | Fetch remotes before computing ahead/behind counts |
| `--concurrency` | int (≥ 1) | config (`6`) | No | — | Worker count |
| `--timeout` | int (≥ 1) | config (`60`) | No | — | Per-repo fetch timeout when `--fetch` is set |
| `--include-worktrees` | bool | config (`false`) | No | — | Include linked worktrees |

### `repo-updater config`

- `config init`: `--dry-run`, `-y/--yes` (accept defaults without prompting).
- `config view`: `--show-origin` (annotate each resolved setting with its source layer).
- `config path`: global options only.
- `config get <key>`: global options only.
- `config set <key> <value>`: `--dry-run`, `-y/--yes`.

### `repo-updater doctor`

- `--bundle`: emit a redacted JSON/text diagnostic snapshot safe for public issues.

---

## 4. Configuration & Environment Precedence (§5)

- **Canonical filename (Decision #47):** `repo-updater_config.toml` (also honoring `repo_updater_config.toml` during migration).
- **Layered discovery (lowest → highest):**
  1. Built-in defaults
  2. System (`$XDG_CONFIG_DIRS/repo-updater/repo-updater_config.toml`)
  3. User (`$XDG_CONFIG_HOME/repo-updater/repo-updater_config.toml`, default `~/.config/repo-updater/repo-updater_config.toml`)
  4. Project (`./.repo-updater/repo-updater_config.toml` or `./repo-updater_config.toml`)
  5. Environment variables (`REPO_UPDATER_*`)
  6. CLI flags (highest)
  - `--config PATH` / `$REPO_UPDATER_CONFIG` overrides file discovery with a single file.
- **Merge rule (Decision #21):** Tables merge recursively; scalars and arrays (including `[[roots]]` and `exclude`) replace from nearer scope.
- **Environment variables:** `REPO_UPDATER_CONFIG`, `REPO_UPDATER_OUTPUT`, `REPO_UPDATER_LOG_LEVEL`, `REPO_UPDATER_LOG_FILE`, `REPO_UPDATER_LOG_FORMAT`, `REPO_UPDATER_CONCURRENCY`, `REPO_UPDATER_TIMEOUT`, `REPO_UPDATER_PAGER`, `NO_COLOR`.

---

## 5. Output & Error Contract (§7)

- **Streams (R7.1, R7.6):** Requested results, `--help`, and `--version` go to **stdout**; logs, progress indicators, prompts, warnings, and errors go to **stderr**.
- **Deterministic ordering (Decision #39):** Discovered repositories are sorted by `(root_index, relative_path)` so output is reproducible across parallel runs.
- **Machine error schema (R7.8, Decision #33, #48):** Under `-o json`/`--json`, every failure emits one JSON object on stderr:
  ```json
  {
    "error": {
      "code": 2,
      "name": "CONFIG",
      "error_code": "REPO_UPDATER001",
      "message": "No configuration file found and no --root provided.",
      "hint": "Run 'repo-updater config init' or pass '--root <path>'."
    }
  }
  ```
- **Schema stability (Decision #50):** JSON fields are append-only within a major version.

---

## 6. Exit Codes & Signals (§6, R9.6)

| Exit code | Meaning |
|---|---|
| `0` | Success (all repositories updated, up-to-date, or cleanly skipped) |
| `1` | Partial or total operational failure (fetch error, timeout, dirty-blocked FF, stash conflict) — continue-on-error by default (R6.3, Decision #37) |
| `2` | CLI usage error or missing/invalid configuration (R6.1, Decision #16) |
| `4` | Unexpected internal error (traceback appended to XDG state crash log) |
| `130` | Interrupted by `SIGINT` (cancels pending workers cleanly) |
| `143` | Terminated by `SIGTERM` (cancels pending workers cleanly, Decision #38) |

---

## 7. Safety & Non-Interactive Guarantees (§8, §10)

- **Non-interactive git subprocesses:** Every git command runs with `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, `GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=15"`, and `LC_ALL=C`.
- **First-run setup gating (Decision #51):** Missing-config setup prompts fire **only** when stdin and stderr are interactive TTYs and neither `--no-input` nor `--json` is active; otherwise exit `2` with an actionable hint.
- **No unnecessary working-tree mutation:** Already-current (`behind == 0`), ahead (`ahead > 0`), and diverged repositories are never stashed or modified. Linked worktrees are skipped by default; off-branch repositories update only the local `<default>` ref without switching branches.
