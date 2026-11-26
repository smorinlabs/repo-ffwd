---
name: repo-ffwd
description: "Safely fetch and fast-forward default branches across local Git repositories in parallel using the `repo-ffwd` CLI. Use when asked to update all local repos, pull the latest main/master across workspaces, check which repos are behind or diverged, or set up multi-repo sync configuration. Not for finding where a repo lives (repo-finder), fixing CI (ci-fix), or merging PRs (pr-merge-flow)."
allowed-tools: Bash, Read
---

# repo-ffwd

Keep local Git repository checkouts synchronized with their remote default
branches safely, fast, and non-interactively via the `repo-ffwd` CLI.

## Resolve the CLI

Check whether `repo-ffwd` is installed on `PATH`; otherwise fall back to
`uvx repo-ffwd`:

```bash
RU="$(command -v repo-ffwd || true)"
if [ -n "$RU" ]; then
  "$RU" --version
else
  uvx repo-ffwd --version
fi
```

Use `"$RU"` (or `uvx repo-ffwd`) consistently for the rest of the session.

## Standard Workflow

### 1. Run a non-interactive parallel sync

Always pass `--json --no-input` when invoking from an agent so stdout is clean
parseable JSON and commands never hang on prompts:

```bash
repo-ffwd repo sync --json --no-input
```

Common variations:

- **Preview without mutating:** `repo-ffwd repo sync --dry-run --json --no-input`
- **Target one configured group:** `repo-ffwd repo sync --tag <tag> --json --no-input`
- **One-off root directory:** `repo-ffwd repo sync --root <path> --max-depth 2 --json --no-input`
- **Tune parallelism / timeout:** `repo-ffwd repo sync --concurrency 8 --timeout 90 --json --no-input`
- **Opt-in conditional AutoStash:** `repo-ffwd repo sync --autostash --json --no-input`
  *(Never stashes repositories that are already up to date or cannot fast-forward; only stashes when behind and dirty files block fast-forward, then pops.)*

### 2. Handle missing configuration (`REPO_FFWD001`)

If `repo sync` exits `1` with error code `REPO_FFWD001` ("No configuration
file with roots found"):

1. Do **not** guess filesystem roots or scan `$HOME` blindly.
2. Ask the user which root directory or directories they want synced, or have
   them run the interactive setup once in their terminal:
   ```bash
   repo-ffwd config init
   ```
3. For a single run without writing config, pass `--root <path>` explicitly.

### 3. Discovery, read-only status, and diagnostics

- **List discovered repos (offline):** `repo-ffwd repo list --json --no-input`
- **Read-only status (offline):** `repo-ffwd repo status --json --no-input`
- **Read-only status with fetch:** `repo-ffwd repo status --fetch --json --no-input`
- **Inspect effective config:** `repo-ffwd config view --show-origin --json`
- **Diagnose environment & roots:** `repo-ffwd doctor --json`

## Reporting & Triage

Summarize the JSON `items` array grouped clearly:

1. **Updated (`updated`, `updated_off_branch`):** list repo, commit delta
   (`old_sha..new_sha`, `+commits_pulled`), and top `recent_commits` subjects.
2. **Up to date (`up_to_date`):** report total count (do not flood the user
   with every unchanged repo unless asked).
3. **Needs attention — never auto-resolve without user approval:**
   - `diverged` — report `ahead`/`behind` counts; ask before rebasing or merging.
   - `ahead_local` — report unpushed local commit count.
   - `blocked_dirty` — report blocking dirty/untracked files; offer re-running
     with `--autostash` or committing/cleaning first.
   - `stash_conflict` — warn that the fast-forward landed and the stash was
     preserved; instruct the user to inspect `git stash list`.
   - `timeout` / `error` — surface stderr detail; if SSH auth failed under
     `BatchMode=yes`, suggest unlocking the SSH agent or switching the remote
     to HTTPS.
   - `skipped` — note linked worktrees (skipped by default to protect shared
     refs/indices) and local-only repos without remotes.

## Safety Rules

- Never force-pull, `git reset --hard`, rebase, or switch branches on behalf of
  the user without explicit confirmation.
- Keep all user-specific roots, excludes, and tags in the user's XDG config
  (`repo-ffwd config path`) — never hardcode personal paths or repo names.
