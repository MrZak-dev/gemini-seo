#!/usr/bin/env python3
"""Deterministic converter and upstream sync helper for the Antigravity fork.

Transforms upstream AgriciDaniel/claude-seo files on the ``antigravity`` branch
into a native Antigravity plugin layout without modifying upstream-owned config
files (``.claude-plugin/*``, ``hooks/hooks.json``, ``AGENTS.md``, ``CLAUDE.md``,
``scripts/*``, or hash-locked ``skills/seo-flow/references/prompts/*``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

LAUNCHER_ROOT_EXPR = "${GEMINI_SEO_ROOT:-$HOME/.gemini/config/plugins/gemini-seo}"
CLAUDE_PLUGIN_ROOT_TOKEN = "${CLAUDE_PLUGIN_ROOT}"
MAX_SKILL_BODY_BYTES = 24000

MODEL_MAP = {
    "opus": "pro",
    "sonnet": "inherit",
    "haiku": "flash",
}
ALLOWED_ANTIGRAVITY_MODELS = {"inherit", "pro", "flash", "flash_lite"}

CLAUDE_TOOL_MAP: dict[str, tuple[str, ...]] = {
    "Read": ("read_file",),
    "Glob": ("read_file",),
    "Grep": ("read_file",),
    "Edit": ("edit_file",),
    "MultiEdit": ("edit_file",),
    "Write": ("write_file",),
    "Bash": ("run_command", "manage_task"),
    "WebSearch": ("search_web",),
    "WebFetch": ("search_web",),
}

NATIVE_ANTIGRAVITY_TOOLS = {
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

MCP_BRIDGE_TOOLS = ("call_mcp_tool", "list_resources", "read_resource")

SUGGESTED_PROMPTS = [
    "Run a full technical and content SEO audit on https://example.com",
    "Analyze Schema.org structured data and Core Web Vitals for https://example.com",
    "Audit AI search (GEO) and agent readiness for https://example.com",
]

HOOK_MATCHER = (
    "edit_file|write_file|replace_file_content|multi_replace_file_content|write_to_file"
)
HOOK_COMMAND = "node ./tools/antigravity-schema-hook.mjs"

RUNTIME_PARAGRAPH_ANTIGRAVITY = (
    "**Runtime:** Run bundled Python tools through\n"
    f'`"{LAUNCHER_ROOT_EXPR}/scripts/claude-seo" run <script.py>`.\n'
    "That is the canonical form used by every skill and agent in Antigravity (set\n"
    "`GEMINI_SEO_ROOT` if installed outside `$HOME/.gemini/config/plugins/gemini-seo`,\n"
    "or invoke `../../scripts/claude-seo` relative to a loaded skill directory).\n"
    "Repository users run `./scripts/claude-seo`. Never invoke bundled scripts with\n"
    "a bare Python interpreter."
)

# Paths owned by the Antigravity fork that must never be overwritten blindly by a
# merge from upstream/main.
ANTIGRAVITY_OWNED_PREFIXES = (
    "tools/",
    "rules/",
    "assets/logo.svg",
    "ANTIGRAVITY.md",
    "tests/test_antigravity_plugin.py",
    ".github/workflows/antigravity.yml",
)

LOCKED_FLOW_PROMPTS_PREFIX = Path("skills/seo-flow/references/prompts")
LOCKED_FLOW_FILE = Path("skills/seo-flow/references/flow-prompts.lock")


def split_frontmatter(text: str, source_label: str) -> tuple[str, str]:
    """Split a Markdown file into ``(frontmatter, body)``."""
    match = re.match(r"^---[ \t]*\n(.*?)\n---[ \t]*\n(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError(f"{source_label}: missing or malformed YAML frontmatter")
    return match.group(1), match.group(2)


def _strip_yaml_comment(value: str) -> str:
    """Remove an unquoted trailing ``# ...`` YAML comment."""
    in_single = False
    in_double = False
    for idx, ch in enumerate(value):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or value[idx - 1].isspace():
                return value[:idx].rstrip()
    return value.rstrip()


def _unquote_yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        inner = value[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _parse_bool(value: str) -> bool:
    lowered = _unquote_yaml_scalar(_strip_yaml_comment(value)).strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise ValueError(f"Expected boolean YAML value, got {value!r}")


def parse_top_level_blocks(fm_text: str) -> list[tuple[str, list[str]]]:
    """Group YAML frontmatter lines by top-level key without losing formatting."""
    blocks: list[tuple[str, list[str]]] = []
    current_key: str | None = None
    current_lines: list[str] = []

    for line in fm_text.splitlines():
        if line and not line[0].isspace() and not line.lstrip().startswith("#") and ":" in line:
            if current_key is not None:
                blocks.append((current_key, current_lines))
            current_key = line.split(":", 1)[0].strip()
            current_lines = [line]
        else:
            if current_key is not None:
                current_lines.append(line)
            elif line.strip() and not line.lstrip().startswith("#"):
                raise ValueError(f"Unexpected frontmatter line before key: {line!r}")

    if current_key is not None:
        blocks.append((current_key, current_lines))
    return blocks


def _extract_block_scalar_or_value(lines: list[str]) -> str:
    first_line = lines[0]
    raw_after = first_line.split(":", 1)[1].strip()
    indicator = _strip_yaml_comment(raw_after).strip()

    if indicator in (">", ">-", "|", "|-"):
        body_lines = [ln.strip() for ln in lines[1:]]
        while body_lines and not body_lines[-1]:
            body_lines.pop()
        if indicator.startswith(">"):
            # Fold consecutive non-empty lines with spaces, preserve blank-line paragraphs.
            paragraphs: list[str] = []
            buf: list[str] = []
            for ln in body_lines:
                if not ln:
                    if buf:
                        paragraphs.append(" ".join(buf))
                        buf = []
                else:
                    buf.append(ln)
            if buf:
                paragraphs.append(" ".join(buf))
            return "\n\n".join(paragraphs).strip()
        return "\n".join(body_lines).strip()

    parts = [_unquote_yaml_scalar(_strip_yaml_comment(raw_after))]
    for extra in lines[1:]:
        stripped = extra.strip()
        if stripped and not stripped.startswith("#"):
            parts.append(stripped)
    return " ".join(p for p in parts if p).strip()


def _parse_agent_tools(lines: list[str]) -> list[str]:
    raw_after = _strip_yaml_comment(lines[0].split(":", 1)[1]).strip()
    tokens: list[str] = []

    if raw_after:
        if raw_after.startswith("[") and raw_after.endswith("]"):
            raw_after = raw_after[1:-1]
        for item in raw_after.split(","):
            cleaned = _unquote_yaml_scalar(_strip_yaml_comment(item)).strip()
            if cleaned:
                tokens.append(cleaned)
    else:
        for extra in lines[1:]:
            stripped = _strip_yaml_comment(extra).strip()
            if not stripped:
                continue
            if stripped.startswith("-"):
                item = _unquote_yaml_scalar(stripped[1:].strip()).strip()
                if item:
                    tokens.append(item)
            else:
                for item in stripped.split(","):
                    cleaned = _unquote_yaml_scalar(item).strip()
                    if cleaned:
                        tokens.append(cleaned)
    return tokens


def _map_agent_tools(tokens: list[str], source_label: str) -> tuple[list[str], bool]:
    mapped: list[str] = []
    has_mcp = False

    def add_tool(name: str) -> None:
        if name not in mapped:
            mapped.append(name)

    for tok in tokens:
        if tok.startswith("mcp__"):
            has_mcp = True
            for bridge in MCP_BRIDGE_TOOLS:
                add_tool(bridge)
        elif tok in CLAUDE_TOOL_MAP:
            for target in CLAUDE_TOOL_MAP[tok]:
                add_tool(target)
        elif tok in NATIVE_ANTIGRAVITY_TOOLS:
            if tok == "call_mcp_tool":
                has_mcp = True
            add_tool(tok)
        else:
            raise ValueError(f"{source_label}: unrecognized agent tool token {tok!r}")

    return mapped, has_mcp


def _format_yaml_description(description: str) -> list[str]:
    cleaned = " ".join(description.split())
    if len(cleaned) <= 78 and ":" not in cleaned and not cleaned.startswith(("-", "[", "{", "*", "&", "!", "|", ">", "#", "'", '"')):
        return [f"description: {cleaned}"]
    wrapped = textwrap.wrap(
        cleaned,
        width=78,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return ["description: >-"] + [f"  {line}" for line in wrapped]


def build_plugin_json(repo_root: Path) -> str:
    """Build root ``plugin.json`` from ``.claude-plugin/plugin.json``."""
    source_path = repo_root / ".claude-plugin" / "plugin.json"
    src = json.loads(source_path.read_text(encoding="utf-8"))

    description = str(src["description"]).replace(
        "for Claude Code", "for Antigravity and Gemini"
    )

    keywords: list[str] = []
    for kw in ["antigravity", "gemini", *src.get("keywords", [])]:
        if isinstance(kw, str) and kw not in keywords:
            keywords.append(kw)

    author_src = src.get("author") or {}
    if isinstance(author_src, dict):
        author: str | dict[str, str] = {}
        if author_src.get("name"):
            author["name"] = str(author_src["name"])
        if author_src.get("url"):
            author["url"] = str(author_src["url"])
    else:
        author = str(author_src)

    manifest = {
        "$schema": "https://antigravity.google/schemas/v1/plugin.json",
        "name": "gemini-seo",
        "displayName": "Gemini SEO",
        "version": str(src["version"]),
        "description": description,
        "logo": "assets/logo.svg",
        "suggestedPrompts": list(SUGGESTED_PROMPTS),
        "author": author,
        "homepage": "https://github.com/MrZak-dev/gemini-seo",
        "repository": "https://github.com/MrZak-dev/gemini-seo",
        "license": str(src.get("license", "MIT")),
        "keywords": keywords,
    }
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def build_hooks_json(repo_root: Path) -> str:
    """Build root ``hooks.json`` from upstream ``hooks/hooks.json``."""
    upstream_hooks_path = repo_root / "hooks" / "hooks.json"
    upstream = json.loads(upstream_hooks_path.read_text(encoding="utf-8"))

    timeout = 30
    post_tool = (upstream.get("hooks") or {}).get("PostToolUse") or []
    if post_tool and isinstance(post_tool, list):
        handlers = post_tool[0].get("hooks") or []
        if handlers and isinstance(handlers[0], dict):
            raw_timeout = handlers[0].get("timeout")
            if isinstance(raw_timeout, int) and raw_timeout > 0:
                timeout = raw_timeout

    hooks_doc = {
        "validate-schema": {
            "PostToolUse": [
                {
                    "matcher": HOOK_MATCHER,
                    "hooks": [
                        {
                            "type": "command",
                            "command": HOOK_COMMAND,
                            "timeout": timeout,
                        }
                    ],
                }
            ]
        }
    }
    return json.dumps(hooks_doc, indent=2) + "\n"


def transform_agent_text(text: str, agent_path: Path) -> str:
    """Convert an upstream or previously converted ``agents/*.md`` file."""
    fm_text, body = split_frontmatter(text, str(agent_path))
    blocks = parse_top_level_blocks(fm_text)
    block_map = {key: lines for key, lines in blocks}

    name = (
        _extract_block_scalar_or_value(block_map["name"])
        if "name" in block_map
        else agent_path.stem
    )
    description = (
        _extract_block_scalar_or_value(block_map["description"])
        if "description" in block_map
        else ""
    )
    if not description:
        raise ValueError(f"{agent_path}: agent description must be non-empty")

    raw_model = (
        _extract_block_scalar_or_value(block_map["model"])
        if "model" in block_map
        else "inherit"
    )
    if raw_model in MODEL_MAP:
        model = MODEL_MAP[raw_model]
    elif raw_model in ALLOWED_ANTIGRAVITY_MODELS:
        model = raw_model
    else:
        raise ValueError(f"{agent_path}: unrecognized agent model {raw_model!r}")

    if "tools" in block_map:
        raw_tools = _parse_agent_tools(block_map["tools"])
        mapped_tools, inferred_mcp = _map_agent_tools(raw_tools, str(agent_path))
    else:
        mapped_tools = [
            "read_file",
            "edit_file",
            "write_file",
            "run_command",
            "manage_task",
            "search_web",
        ]
        inferred_mcp = False

    if "inheritMcp" in block_map and not inferred_mcp:
        inherit_mcp = _parse_bool(block_map["inheritMcp"][0].split(":", 1)[1])
    else:
        inherit_mcp = inferred_mcp

    out_lines = [
        "---",
        f"name: {name}",
        *_format_yaml_description(description),
        f"model: {model}",
        "mainAgent: false",
        "subagent: true",
        f"inheritMcp: {'true' if inherit_mcp else 'false'}",
        "tools:",
        *[f"  - {tool}" for tool in mapped_tools],
        "---",
    ]

    new_body = body.replace(CLAUDE_PLUGIN_ROOT_TOKEN, LAUNCHER_ROOT_EXPR)
    return "\n".join(out_lines) + "\n" + new_body


def transform_skill_text(
    text: str,
    skill_path: Path,
    strip_frontmatter_keys: tuple[str, ...] = (),
) -> tuple[str, int]:
    """Convert a ``skills/*/SKILL.md`` file and return ``(new_text, body_bytes)``."""
    fm_text, body = split_frontmatter(text, str(skill_path))
    blocks = parse_top_level_blocks(fm_text)
    block_map = {key: lines for key, lines in blocks}

    expected_name = skill_path.parent.name
    actual_name = (
        _extract_block_scalar_or_value(block_map["name"])
        if "name" in block_map
        else ""
    )
    if actual_name != expected_name:
        raise ValueError(
            f"{skill_path}: frontmatter name {actual_name!r} != directory {expected_name!r}"
        )

    description = (
        _extract_block_scalar_or_value(block_map["description"])
        if "description" in block_map
        else ""
    )
    if not description:
        raise ValueError(f"{skill_path}: frontmatter description must be non-empty")

    kept_lines: list[str] = []
    strip_set = set(strip_frontmatter_keys)
    for key, lines in blocks:
        if key in strip_set:
            continue
        kept_lines.extend(lines)

    new_body = body
    if expected_name == "seo":
        new_body = new_body.replace(
            "**Invocation:** `/seo $ARGUMENTS`, where",
            "**Invocation:** `/seo <command> [url]`, where",
        )
        new_body = re.sub(
            r"\*\*Runtime:\*\* Run bundled Python tools through.*?with a bare Python interpreter\.",
            RUNTIME_PARAGRAPH_ANTIGRAVITY,
            new_body,
            count=1,
            flags=re.DOTALL,
        )

    new_body = new_body.replace(CLAUDE_PLUGIN_ROOT_TOKEN, LAUNCHER_ROOT_EXPR)
    body_bytes = len(new_body.encode("utf-8"))
    if body_bytes > MAX_SKILL_BODY_BYTES:
        print(
            f"WARNING: {skill_path} body is {body_bytes} bytes "
            f"(limit {MAX_SKILL_BODY_BYTES} bytes)",
            file=sys.stderr,
        )

    rebuilt_fm = "\n".join(kept_lines)
    return f"---\n{rebuilt_fm}\n---\n{new_body}", body_bytes


def _is_locked_flow_path(rel_path: Path) -> bool:
    return rel_path == LOCKED_FLOW_FILE or rel_path.is_relative_to(LOCKED_FLOW_PROMPTS_PREFIX)


def _sync_file(path: Path, content: str, check: bool, drifted: list[str]) -> bool:
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    if existing == content:
        return False
    if check:
        drifted.append(str(path))
        return True
    path.write_text(content, encoding="utf-8")
    return True


def transform_repo(repo_root: Path, check: bool = False) -> dict[str, object]:
    """Generate and transform all Antigravity plugin files in ``repo_root``."""
    drifted: list[str] = []
    updated: list[str] = []
    max_skill_bytes = 0
    max_skill_name = ""

    def record(path: Path, content: str) -> None:
        if _sync_file(path, content, check=check, drifted=drifted):
            updated.append(path.relative_to(repo_root).as_posix())

    record(repo_root / "plugin.json", build_plugin_json(repo_root))
    record(repo_root / "hooks.json", build_hooks_json(repo_root))

    agent_files = sorted((repo_root / "agents").glob("*.md"))
    for agent_path in agent_files:
        original = agent_path.read_text(encoding="utf-8")
        converted = transform_agent_text(original, agent_path)
        record(agent_path, converted)

    skill_files = sorted((repo_root / "skills").glob("*/SKILL.md"))
    for skill_path in skill_files:
        original = skill_path.read_text(encoding="utf-8")
        converted, body_bytes = transform_skill_text(original, skill_path)
        if body_bytes > max_skill_bytes:
            max_skill_bytes = body_bytes
            max_skill_name = skill_path.parent.name
        record(skill_path, converted)

    ref_files = sorted((repo_root / "skills").glob("*/references/**/*.md"))
    for ref_path in ref_files:
        rel_path = ref_path.relative_to(repo_root)
        if _is_locked_flow_path(rel_path):
            continue
        original = ref_path.read_text(encoding="utf-8")
        if CLAUDE_PLUGIN_ROOT_TOKEN in original:
            converted = original.replace(CLAUDE_PLUGIN_ROOT_TOKEN, LAUNCHER_ROOT_EXPR)
            record(ref_path, converted)

    return {
        "drifted": drifted,
        "updated": updated,
        "skill_count": len(skill_files),
        "agent_count": len(agent_files),
        "max_skill_bytes": max_skill_bytes,
        "max_skill_name": max_skill_name,
    }


def _git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _is_antigravity_owned(path_str: str) -> bool:
    return any(
        path_str == prefix or path_str.startswith(prefix)
        for prefix in ANTIGRAVITY_OWNED_PREFIXES
    )


def sync_from_git_ref(repo_root: Path, git_ref: str) -> int:
    """Merge ``git_ref`` (e.g., ``main``) and re-apply Antigravity transforms."""
    dirty = _git(repo_root, "status", "--porcelain").stdout.strip()
    if dirty:
        print("Error: working tree must be clean before --sync-from.", file=sys.stderr)
        return 1

    branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch == "main":
        print("Error: switch off 'main' (e.g. 'git checkout antigravity') first.", file=sys.stderr)
        return 1

    merge_proc = _git(repo_root, "merge", "--no-commit", "-X", "theirs", git_ref, check=False)
    status_lines = [
        line for line in _git(repo_root, "status", "--porcelain").stdout.splitlines() if line
    ]

    unmerged: list[tuple[str, str]] = []
    for line in status_lines:
        code = line[:2]
        rel = line[3:].strip()
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1]
        if code in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
            unmerged.append((code, rel))

    for code, rel in unmerged:
        if _is_antigravity_owned(rel):
            _git(repo_root, "merge", "--abort", check=False)
            print(
                f"Error: merge conflict in Antigravity-owned file {rel} ({code}); aborted.",
                file=sys.stderr,
            )
            return 1

    remaining_conflicts: list[tuple[str, str]] = []
    for code, rel in unmerged:
        if code == "DU" and rel.startswith(("skills/", "agents/")):
            _git(repo_root, "rm", "-f", rel)
        else:
            remaining_conflicts.append((code, rel))

    if remaining_conflicts:
        _git(repo_root, "merge", "--abort", check=False)
        details = ", ".join(f"{rel} ({code})" for code, rel in remaining_conflicts)
        print(f"Error: unresolved merge conflicts: {details}; aborted.", file=sys.stderr)
        return 1

    if merge_proc.returncode not in (0, 1):
        _git(repo_root, "merge", "--abort", check=False)
        print(merge_proc.stderr.strip() or "git merge failed", file=sys.stderr)
        return 1

    summary = transform_repo(repo_root, check=False)
    print(
        f"Synced from {git_ref}: updated {len(summary['updated'])} files "
        f"({summary['skill_count']} skills, {summary['agent_count']} agents, "
        f"max skill body {summary['max_skill_bytes']}B in {summary['max_skill_name']})."
    )

    if shutil.which("agy"):
        val = subprocess.run(
            ["agy", "plugin", "validate", str(repo_root)],
            capture_output=True,
            text=True,
            check=False,
        )
        print(val.stdout.rstrip())
        if val.returncode != 0:
            print(val.stderr.rstrip(), file=sys.stderr)
            print("Warning: agy plugin validate reported issues.", file=sys.stderr)

    print("Next steps: inspect `git status`, run tests, and commit the merge.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert claude-seo files to Antigravity plugin format."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any converted file has drifted from generated output.",
    )
    parser.add_argument(
        "--sync-from",
        metavar="REF",
        help="Merge upstream changes from REF (e.g. main) and re-run conversion.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Path to the repository root (defaults to parent of tools/).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()

    if args.sync_from:
        if args.check:
            print("Error: --check and --sync-from cannot be used together.", file=sys.stderr)
            return 2
        return sync_from_git_ref(repo_root, args.sync_from)

    summary = transform_repo(repo_root, check=args.check)
    if args.check:
        drifted = summary["drifted"]
        if drifted:
            print("Drifted Antigravity plugin files:", file=sys.stderr)
            for item in drifted:
                print(f"  - {item}", file=sys.stderr)
            return 1
        print(
            f"Check passed: {summary['skill_count']} skills, "
            f"{summary['agent_count']} agents, max skill body "
            f"{summary['max_skill_bytes']}B ({summary['max_skill_name']})."
        )
        return 0

    print(
        f"Converted repository: {len(summary['updated'])} files written "
        f"({summary['skill_count']} skills, {summary['agent_count']} agents, "
        f"max skill body {summary['max_skill_bytes']}B in {summary['max_skill_name']})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
