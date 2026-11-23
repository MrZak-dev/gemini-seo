---
trigger: model_decision
description: Runtime path resolution, subagent delegation, and tool conventions for Gemini SEO skills and subagents. Load when executing any seo or seo-* SEO analysis workflow.
---

# Gemini SEO Runtime Conventions

- **Bundled Python launcher**: Run bundled Python scripts through `"${GEMINI_SEO_ROOT:-$HOME/.gemini/config/plugins/gemini-seo}/scripts/claude-seo" run <script.py>` (or `setup` / `doctor`). Never invoke bundled scripts with a bare Python interpreter.
- **Custom or workspace plugin installs**: If the plugin is installed in a workspace `.agents/plugins/` folder or another custom path instead of `$HOME/.gemini/config/plugins/gemini-seo`, resolve `scripts/claude-seo` two directories above any loaded `SKILL.md` (`../../scripts/claude-seo`) or set `GEMINI_SEO_ROOT` to that plugin root directory.
- **Subagent delegation**: Delegate specialized phases to the bundled `seo-*` subagents via `invoke_subagent`. Respect each subagent's declared tool boundaries: MCP-only analysts (`seo-dataforseo`, `seo-posthog`) do not run shell commands, and shell-enabled web analysts (`seo-maps`) do not inherit workspace MCP tools.
- **Tool name mapping**: When upstream reference files mention Claude Code tool names, use their native Antigravity equivalents: `Read`/`Glob`/`Grep` -> `read_file`, `Edit` -> `edit_file`, `Write` -> `write_file`, `Bash` -> `run_command` / `manage_task`, and `WebSearch`/`WebFetch` -> `search_web`.
