"""End-to-end CLI tests for `repo-updater repo`, `config view`, and `doctor`."""

import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from repo_updater.cli.main import cli

_GIT = shutil.which("git") or "git"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603
        [_GIT, "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _make_remote_and_clone(tmp_path: Path, name: str) -> tuple[Path, Path]:
    bare = tmp_path / "remotes" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        [_GIT, "init", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
    )
    upstream = tmp_path / "upstream" / name
    subprocess.run(  # noqa: S603
        [_GIT, "clone", str(bare), str(upstream)],
        check=True,
        capture_output=True,
    )
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "Test User")
    (upstream / "README.md").write_text("v1\n", encoding="utf-8")
    _git(upstream, "add", "README.md")
    _git(upstream, "commit", "-m", "initial")
    _git(upstream, "push", "-u", "origin", "main")

    clone = tmp_path / "workspace" / name
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        [_GIT, "clone", str(bare), str(clone)],
        check=True,
        capture_output=True,
    )
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test User")
    return upstream, clone


@pytest.fixture
def runner():
    return CliRunner()


def test_repo_sync_and_list_json_with_root_flag(runner, tmp_path):
    upstream, clone = _make_remote_and_clone(tmp_path, "demo")
    (upstream / "a.txt").write_text("a\n", encoding="utf-8")
    _git(upstream, "add", "a.txt")
    _git(upstream, "commit", "-m", "feat: add a")
    _git(upstream, "push")

    workspace = tmp_path / "workspace"
    list_res = runner.invoke(cli, ["repo", "list", "--root", str(workspace), "--json"])
    assert list_res.exit_code == 0, list_res.output
    listed = json.loads(list_res.output)
    assert [r["name"] for r in listed["repos"]] == ["demo"]

    # Plural alias `repos` works identically (Decision #5).
    alias_res = runner.invoke(
        cli, ["repos", "list", "--root", str(workspace), "--json"]
    )
    assert alias_res.exit_code == 0, alias_res.output
    assert json.loads(alias_res.output)["repos"][0]["name"] == "demo"

    # Dry-run previews without updating.
    dry_res = runner.invoke(
        cli,
        ["repo", "sync", "--root", str(workspace), "--dry-run", "--json"],
    )
    assert dry_res.exit_code == 0, dry_res.output
    assert json.loads(dry_res.output)["items"][0]["outcome"] == "would_update"

    # Real sync updates cleanly.
    sync_res = runner.invoke(
        cli,
        ["repo", "sync", "--root", str(workspace), "--concurrency", "2", "--json"],
    )
    assert sync_res.exit_code == 0, sync_res.output
    payload = json.loads(sync_res.output)
    assert payload["items"][0]["outcome"] == "updated"
    assert payload["items"][0]["commits_pulled"] == 1
    assert (clone / "a.txt").exists()


def test_repo_sync_missing_config_refuses_non_interactive(runner, tmp_path):
    missing = tmp_path / "missing.toml"
    for flag in (["--no-input"], ["--json"], ["-o", "json"]):
        res = runner.invoke(cli, ["repo", "sync", "--config", str(missing), *flag])
        assert res.exit_code != 0
        assert "config init" in res.output


def test_repo_sync_interactive_first_run_setup_creates_config(runner, tmp_path):
    upstream, _ = _make_remote_and_clone(tmp_path, "first-run")
    (upstream / "b.txt").write_text("b\n", encoding="utf-8")
    _git(upstream, "add", "b.txt")
    _git(upstream, "commit", "-m", "feat: add b")
    _git(upstream, "push")

    cfg_path = tmp_path / "new_config.toml"
    workspace = tmp_path / "workspace"
    # Simulate interactive TTY stdin + stderr so missing-config setup triggers.
    answers = f"y\n{workspace}\n2\n4\n30\nfalse\ntext\nauto\nwarning\n"
    with (
        patch("repo_updater.cli.commands.repo._stdin_is_tty", return_value=True),
        patch("rich.console.Console.is_terminal", True),
    ):
        res = runner.invoke(
            cli,
            ["repo", "sync", "--config", str(cfg_path)],
            input=answers,
        )
    assert res.exit_code == 0, res.output
    assert cfg_path.exists()
    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["roots"] == [{"path": str(workspace)}]
    assert data["sync"]["concurrency"] == 4
    assert data["sync"]["timeout_seconds"] == 30


def test_repo_sync_partial_failure_exits_one(runner, tmp_path):
    upstream, clone = _make_remote_and_clone(tmp_path, "blocked")
    (upstream / "README.md").write_text("upstream\n", encoding="utf-8")
    _git(upstream, "add", "README.md")
    _git(upstream, "commit", "-m", "docs: upstream")
    _git(upstream, "push")
    (clone / "README.md").write_text("dirty local\n", encoding="utf-8")

    res = runner.invoke(
        cli,
        ["repo", "sync", "--root", str(tmp_path / "workspace"), "--json"],
    )
    assert res.exit_code == 1
    payload = json.loads(res.output)
    assert payload["items"][0]["outcome"] == "blocked_dirty"


def test_repo_status_command(runner, tmp_path):
    _make_remote_and_clone(tmp_path, "stat-repo")
    res = runner.invoke(
        cli,
        ["repo", "status", "--root", str(tmp_path / "workspace"), "--json"],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["fetched"] is False
    assert payload["items"][0]["name"] == "stat-repo"
    assert payload["items"][0]["branch"] == "main"


def test_config_view_and_doctor_with_roots(runner, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = tmp_path / "cfg.toml"
    init_res = runner.invoke(
        cli,
        ["config", "init", "--root", str(workspace), "--yes", "--config", str(cfg)],
    )
    assert init_res.exit_code == 0, init_res.output

    view_res = runner.invoke(
        cli, ["config", "view", "--show-origin", "--config", str(cfg), "--json"]
    )
    assert view_res.exit_code == 0, view_res.output
    entries = {e["key"]: e for e in json.loads(view_res.output)["entries"]}
    assert entries["roots"]["value"] == str(workspace)
    assert entries["roots"]["origin"] == "config"

    doc_res = runner.invoke(cli, ["doctor", "--config", str(cfg), "--json"])
    assert doc_res.exit_code == 0, doc_res.output
    checks = {c["name"]: c["status"] for c in json.loads(doc_res.output)["checks"]}
    assert checks["git"] == "ok"
    assert checks["roots"] == "ok"
