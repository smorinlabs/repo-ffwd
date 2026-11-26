"""CLI Design Standard v1.4.15 R9.14 conformance fixtures."""

import json

import pytest
from click.testing import CliRunner

from repo_ffwd import __version__
from repo_ffwd.cli.main import cli


@pytest.fixture
def runner():
    return CliRunner()


def test_r4_1_help_and_version_stdout_exit_zero(runner):
    for flag in ("-h", "--help"):
        res = runner.invoke(cli, [flag])
        assert res.exit_code == 0
        assert "repo" in res.output
        assert "config" in res.output

    for flag in ("-V", "--version"):
        res = runner.invoke(cli, [flag])
        assert res.exit_code == 0
        assert f"repo-ffwd {__version__}" in res.output


def test_r7_9_bare_invocation_and_incomplete_group(runner):
    bare = runner.invoke(cli, [])
    assert bare.exit_code == 0
    assert "Usage:" in bare.output

    incomplete = runner.invoke(cli, ["repo"])
    assert incomplete.exit_code == 2
    assert "Usage:" in incomplete.output


def test_decision_34_unknown_command_suggests_match(runner):
    res = runner.invoke(cli, ["rpo"])
    assert res.exit_code == 2
    assert "Did you mean 'repo'?" in res.output

    res_verb = runner.invoke(cli, ["repo", "snc"])
    assert res_verb.exit_code == 2
    assert "Did you mean 'sync'?" in res_verb.output


def test_decision_5_plural_alias_undocumented_in_help(runner):
    help_res = runner.invoke(cli, ["--help"])
    assert "\n  repos " not in help_res.output
    alias_res = runner.invoke(cli, ["repos", "--help"])
    assert alias_res.exit_code == 0
    assert "sync" in alias_res.output


def test_decision_32_no_flag_prefix_abbreviation(runner):
    res = runner.invoke(cli, ["--ver"])
    assert res.exit_code == 2


def test_r7_8_json_error_schema_equivalence(runner, tmp_path):
    missing = tmp_path / "none.toml"
    for flags in (["--json"], ["-o", "json"]):
        res = runner.invoke(cli, ["repo", "list", "--config", str(missing), *flags])
        assert res.exit_code != 0
        payload = json.loads(res.output)
        assert "error" in payload
        assert "code" in payload["error"]
        assert "message" in payload["error"]
        assert "error_code" in payload["error"]
