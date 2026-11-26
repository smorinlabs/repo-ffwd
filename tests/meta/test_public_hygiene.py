"""Public hygiene and bundled plugin contract tests for repo-ffwd."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "repo-ffwd"
SKILL_PATH = PLUGIN_ROOT / "skills" / "repo-ffwd" / "SKILL.md"


def _project_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def test_bundled_plugin_manifests_exist_and_match_version() -> None:
    expected_version = _project_version()
    for rel in (
        ".antigravity-plugin/plugin.json",
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
    ):
        manifest_path = PLUGIN_ROOT / rel
        assert manifest_path.is_file(), f"missing {manifest_path}"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["name"] == "repo-ffwd"
        assert manifest["version"] == expected_version

    claude = json.loads(
        (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    codex = json.loads(
        (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert claude["skills"] == "./skills/"
    assert codex["skills"] == "./skills/"


def test_bundled_skill_frontmatter_is_valid() -> None:
    assert SKILL_PATH.is_file()
    text = SKILL_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert text.startswith("---\n")
    _, frontmatter, body = text.split("---\n", 2)
    assert "name: repo-ffwd\n" in frontmatter
    assert "description:" in frontmatter
    assert "repo-ffwd repo sync" in body


def test_public_surfaces_contain_no_private_paths_or_aliases() -> None:
    # Construct personal/private markers dynamically so this test stays generic.
    forbidden = [
        "/" + "Users" + "/",
        "~" + "/c/",
        "smorin" + "-skills",
        "smorin" + "-bootstrap",
        "flox" + "_repos",
        "repo-pull-" + "default",
    ]
    scan_paths: list[Path] = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONFORMANCE.md",
        REPO_ROOT / "docs" / "cli-interface.md",
        *sorted((REPO_ROOT / "src" / "repo_ffwd").rglob("*.py")),
        *sorted(PLUGIN_ROOT.rglob("*")),
    ]
    for path in scan_paths:
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in content, (
                f"private reference {token!r} leaked into {path.relative_to(REPO_ROOT)}"
            )
