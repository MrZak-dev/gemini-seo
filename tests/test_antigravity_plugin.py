"""Integration and regression tests for the Antigravity gemini-seo plugin."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = REPO_ROOT / "tools" / "sync_antigravity.py"
HOOK_ADAPTER = REPO_ROOT / "tools" / "antigravity-schema-hook.mjs"
LAUNCHER = REPO_ROOT / "scripts" / "claude-seo"
MAX_SKILL_BODY_BYTES = 24000

CLAUDE_ONLY_TOOLS = {
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "Bash",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
}
ALLOWED_ANTIGRAVITY_MODELS = {"inherit", "pro", "flash", "flash_lite"}
ALLOWED_ANTIGRAVITY_TOOLS = {
    "read_file",
    "edit_file",
    "write_file",
    "run_command",
    "manage_task",
    "search_web",
    "call_mcp_tool",
    "list_resources",
    "read_resource",
    "send_message",
}


def _load_sync_module():
    spec = importlib.util.spec_from_file_location("sync_antigravity", SYNC_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_agent_frontmatter(path: Path) -> tuple[dict[str, object], str]:
    sync = _load_sync_module()
    fm_text, body = sync.split_frontmatter(path.read_text(encoding="utf-8"), str(path))
    blocks = sync.parse_top_level_blocks(fm_text)
    parsed: dict[str, object] = {}
    for key, lines in blocks:
        if key == "tools":
            parsed["tools"] = sync._parse_agent_tools(lines)
        elif key in ("mainAgent", "subagent", "inheritMcp"):
            parsed[key] = sync._parse_bool(lines[0].split(":", 1)[1])
        else:
            parsed[key] = sync._extract_block_scalar_or_value(lines)
    return parsed, body


class TestAntigravityPlugin(unittest.TestCase):
    def test_plugin_manifest_contract(self) -> None:
        manifest_path = REPO_ROOT / "plugin.json"
        upstream_path = REPO_ROOT / ".claude-plugin" / "plugin.json"
        self.assertTrue(manifest_path.is_file(), "plugin.json missing at repo root")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        upstream = json.loads(upstream_path.read_text(encoding="utf-8"))

        self.assertEqual(
            manifest.get("$schema"),
            "https://antigravity.google/schemas/v1/plugin.json",
        )
        self.assertEqual(manifest.get("name"), "gemini-seo")
        self.assertEqual(manifest.get("displayName"), "Gemini SEO")
        self.assertEqual(manifest.get("version"), upstream.get("version"))

        skill_count = sum(
            1
            for d in (REPO_ROOT / "skills").iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        )
        agent_count = sum(
            1 for f in (REPO_ROOT / "agents").iterdir() if f.is_file() and f.suffix == ".md"
        )
        self.assertEqual(skill_count, 29)
        self.assertEqual(agent_count, 20)
        self.assertIn(f"{skill_count} sub-skills", manifest["description"])
        self.assertIn(f"{agent_count} sub-agents", manifest["description"])

        logo_rel = manifest.get("logo")
        self.assertIsInstance(logo_rel, str)
        logo_path = REPO_ROOT / logo_rel
        self.assertTrue(logo_path.is_file(), f"Missing logo file: {logo_path}")
        self.assertEqual(logo_path.suffix, ".svg")
        logo_text = logo_path.read_text(encoding="utf-8")
        self.assertIn("<svg", logo_text)
        vb_match = re.search(r'viewBox="0\s+0\s+(\d+)\s+(\d+)"', logo_text)
        self.assertIsNotNone(vb_match, "SVG logo must define square viewBox")
        assert vb_match is not None
        self.assertEqual(vb_match.group(1), vb_match.group(2))

        prompts = manifest.get("suggestedPrompts")
        self.assertIsInstance(prompts, list)
        self.assertGreaterEqual(len(prompts), 1)
        self.assertLessEqual(len(prompts), 3)
        for prompt in prompts:
            self.assertIsInstance(prompt, str)
            self.assertTrue(prompt.strip())

        self.assertTrue(manifest["author"]["url"].startswith("https://"))
        self.assertTrue(manifest["homepage"].startswith("https://"))
        self.assertTrue(manifest["repository"].startswith("https://"))

    def test_skills_contract(self) -> None:
        sync = _load_sync_module()
        skill_files = sorted((REPO_ROOT / "skills").glob("*/SKILL.md"))
        self.assertEqual(len(skill_files), 29)

        for skill_path in skill_files:
            text = skill_path.read_text(encoding="utf-8")
            fm_text, body = sync.split_frontmatter(text, str(skill_path))
            blocks = dict(sync.parse_top_level_blocks(fm_text))

            name = sync._extract_block_scalar_or_value(blocks["name"])
            desc = sync._extract_block_scalar_or_value(blocks["description"])
            self.assertEqual(name, skill_path.parent.name, str(skill_path))
            self.assertTrue(desc.strip(), f"Empty description in {skill_path}")
            self.assertLessEqual(
                len(body.encode("utf-8")),
                MAX_SKILL_BODY_BYTES,
                f"Skill body exceeds 24000 bytes: {skill_path}",
            )
            self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", text, str(skill_path))

        seo_hub = (REPO_ROOT / "skills" / "seo" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("$ARGUMENTS", seo_hub)

        for ref_path in sorted((REPO_ROOT / "skills").glob("*/references/**/*.md")):
            rel_path = ref_path.relative_to(REPO_ROOT)
            if sync._is_locked_flow_path(rel_path):
                continue
            self.assertNotIn(
                "${CLAUDE_PLUGIN_ROOT}",
                ref_path.read_text(encoding="utf-8"),
                str(rel_path),
            )

    def test_agents_contract_and_security_isolation(self) -> None:
        agent_files = sorted((REPO_ROOT / "agents").glob("*.md"))
        self.assertEqual(len(agent_files), 20)

        for agent_path in agent_files:
            meta, body = _parse_agent_frontmatter(agent_path)
            self.assertEqual(meta.get("name"), agent_path.stem, str(agent_path))
            self.assertTrue(str(meta.get("description", "")).strip(), str(agent_path))
            self.assertIs(meta.get("subagent"), True, str(agent_path))
            self.assertIs(meta.get("mainAgent"), False, str(agent_path))
            self.assertNotIn("maxTurns", meta, str(agent_path))
            self.assertNotIn("color", meta, str(agent_path))
            self.assertIn(meta.get("model"), ALLOWED_ANTIGRAVITY_MODELS, str(agent_path))

            tools = meta.get("tools")
            self.assertIsInstance(tools, list, str(agent_path))
            assert isinstance(tools, list)
            self.assertTrue(tools, f"Empty tools in {agent_path}")
            for tool in tools:
                self.assertNotIn(tool, CLAUDE_ONLY_TOOLS, str(agent_path))
                self.assertFalse(tool.startswith("mcp__"), str(agent_path))
                self.assertIn(tool, ALLOWED_ANTIGRAVITY_TOOLS, str(agent_path))

            self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", body, str(agent_path))

        for mcp_agent in ("seo-dataforseo.md", "seo-posthog.md"):
            meta, _ = _parse_agent_frontmatter(REPO_ROOT / "agents" / mcp_agent)
            self.assertIs(meta["inheritMcp"], True, mcp_agent)
            self.assertNotIn("run_command", meta["tools"], mcp_agent)
            self.assertIn("call_mcp_tool", meta["tools"], mcp_agent)

        maps_meta, _ = _parse_agent_frontmatter(REPO_ROOT / "agents" / "seo-maps.md")
        self.assertIs(maps_meta["inheritMcp"], False)
        self.assertIn("run_command", maps_meta["tools"])
        self.assertNotIn("call_mcp_tool", maps_meta["tools"])

    def test_hooks_json_contract(self) -> None:
        hooks_path = REPO_ROOT / "hooks.json"
        self.assertTrue(hooks_path.is_file(), "hooks.json missing at repo root")
        raw = hooks_path.read_text(encoding="utf-8")
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", raw)
        self.assertNotIn("${PLUGIN_ROOT}", raw)

        data = json.loads(raw)
        self.assertIn("validate-schema", data)
        post_tool = data["validate-schema"]["PostToolUse"]
        self.assertEqual(len(post_tool), 1)
        matcher = re.compile(post_tool[0]["matcher"])
        self.assertIsNotNone(matcher.fullmatch("edit_file"))
        self.assertIsNotNone(matcher.fullmatch("write_file"))

        handlers = post_tool[0]["hooks"]
        self.assertEqual(len(handlers), 1)
        handler = handlers[0]
        self.assertEqual(handler["type"], "command")
        self.assertEqual(handler["command"], "node ./tools/antigravity-schema-hook.mjs")
        self.assertNotIn("args", handler)
        self.assertTrue(HOOK_ADAPTER.is_file())

    def test_hook_adapter_execution(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")

        def run_hook(stdin_text: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [node, str(HOOK_ADAPTER)],
                input=stdin_text,
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                check=False,
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            md_file = tmp_dir / "notes.md"
            md_file.write_text("# Notes\n", encoding="utf-8")

            valid_html = tmp_dir / "valid.html"
            valid_html.write_text(
                '<!doctype html><html><head><script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"Organization","name":"Acme"}'
                "</script></head><body></body></html>\n",
                encoding="utf-8",
            )

            invalid_html = tmp_dir / "invalid.html"
            invalid_html.write_text(
                '<!doctype html><html><head><script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"Organization","name":"[Business Name]"}'
                "</script></head><body></body></html>\n",
                encoding="utf-8",
            )

            res_md = run_hook(
                json.dumps({"toolCall": {"name": "write_file", "args": {"Path": str(md_file)}}})
            )
            self.assertEqual(res_md.returncode, 0)
            self.assertEqual(json.loads(res_md.stdout), {})
            self.assertEqual(res_md.stderr.strip(), "")

            res_valid = run_hook(
                json.dumps({"toolCall": {"name": "write_file", "args": {"Path": str(valid_html)}}})
            )
            self.assertEqual(res_valid.returncode, 0)
            self.assertEqual(json.loads(res_valid.stdout), {})
            self.assertEqual(res_valid.stderr.strip(), "")

            res_invalid = run_hook(
                json.dumps({"toolCall": {"name": "edit_file", "args": {"TargetFile": str(invalid_html)}}})
            )
            self.assertEqual(res_invalid.returncode, 0)
            self.assertEqual(json.loads(res_invalid.stdout), {})
            self.assertIn("placeholder", res_invalid.stderr.lower())

            res_rel = run_hook(
                json.dumps(
                    {
                        "cwd": str(tmp_dir),
                        "toolCall": {"name": "write_file", "args": {"path": "invalid.html"}},
                    }
                )
            )
            self.assertEqual(res_rel.returncode, 0)
            self.assertEqual(json.loads(res_rel.stdout), {})
            self.assertIn("placeholder", res_rel.stderr.lower())

            for bad_input in ("", "not-json", "{"):
                res_bad = run_hook(bad_input)
                self.assertEqual(res_bad.returncode, 0)
                self.assertEqual(json.loads(res_bad.stdout), {})

    def test_runtime_rule_contract(self) -> None:
        rule_path = REPO_ROOT / "rules" / "seo-runtime.md"
        self.assertTrue(rule_path.is_file(), "rules/seo-runtime.md missing")
        raw = rule_path.read_text(encoding="utf-8")
        self.assertLess(len(raw.encode("utf-8")), 24000)
        sync = _load_sync_module()
        fm_text, _ = sync.split_frontmatter(raw, str(rule_path))
        blocks = dict(sync.parse_top_level_blocks(fm_text))
        self.assertEqual(
            sync._extract_block_scalar_or_value(blocks["trigger"]),
            "model_decision",
        )
        self.assertTrue(sync._extract_block_scalar_or_value(blocks["description"]).strip())

    def test_sync_check_and_idempotency(self) -> None:
        check_proc = subprocess.run(
            [sys.executable, str(SYNC_SCRIPT), "--check"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=False,
        )
        self.assertEqual(check_proc.returncode, 0, check_proc.stderr or check_proc.stdout)

        sync = _load_sync_module()
        with tempfile.TemporaryDirectory() as tmp:
            copy_root = Path(tmp) / "repo"
            shutil.copytree(REPO_ROOT / ".claude-plugin", copy_root / ".claude-plugin")
            shutil.copytree(REPO_ROOT / "hooks", copy_root / "hooks")
            shutil.copytree(REPO_ROOT / "agents", copy_root / "agents")
            shutil.copytree(REPO_ROOT / "skills", copy_root / "skills")

            first = sync.transform_repo(copy_root, check=False)
            second = sync.transform_repo(copy_root, check=False)
            self.assertEqual(second["updated"], [], f"Non-idempotent files: {second['updated']}")
            self.assertEqual(first["skill_count"], 29)
            self.assertEqual(first["agent_count"], 20)

    def test_upstream_launcher_smoke(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX bash launcher test")
        env = {**os.environ, "PYTHON_COLORS": "0", "NO_COLOR": "1"}
        env.pop("FORCE_COLOR", None)
        proc = subprocess.run(
            ["bash", str(LAUNCHER), "doctor", "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout)
        self.assertIn("usage: claude-seo doctor", plain)

    def test_no_em_dash_in_antigravity_files(self) -> None:
        em_dash = chr(0x2014)
        targets = [
            REPO_ROOT / "plugin.json",
            REPO_ROOT / "hooks.json",
            REPO_ROOT / "ANTIGRAVITY.md",
            REPO_ROOT / "assets" / "logo.svg",
            REPO_ROOT / "rules" / "seo-runtime.md",
            REPO_ROOT / "tools" / "sync_antigravity.py",
            REPO_ROOT / "tools" / "antigravity-schema-hook.mjs",
            REPO_ROOT / "tests" / "test_antigravity_plugin.py",
            REPO_ROOT / ".github" / "workflows" / "antigravity.yml",
        ]
        for path in targets:
            self.assertTrue(path.is_file(), f"Expected Antigravity file missing: {path}")
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(em_dash, text, f"Em dash found in {path}")

    @unittest.skipUnless(shutil.which("agy"), "agy CLI not installed")
    def test_agy_plugin_validate_json(self) -> None:
        proc = subprocess.run(
            ["agy", "plugin", "validate", "--json", str(REPO_ROOT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        report = json.loads(proc.stdout)
        self.assertEqual(report.get("name"), "gemini-seo")
        self.assertEqual(report.get("manifest"), "plugin.json")

        components = report.get("components", {})
        self.assertEqual(components.get("skills"), 29)
        self.assertEqual(components.get("agents"), 20)
        self.assertEqual(components.get("rules"), 1)
        self.assertEqual(components.get("hooks"), 1)

        allowed_warning_files = {"AGENTS.md", "hooks/hooks.json"}
        unexpected = [
            f
            for f in report.get("findings", [])
            if f.get("severity") == "error"
            or (
                f.get("severity") == "warning"
                and f.get("file") not in allowed_warning_files
            )
        ]
        self.assertEqual(unexpected, [], f"Unexpected agy validation findings: {unexpected}")


if __name__ == "__main__":
    unittest.main()
