# Gemini SEO (Antigravity Plugin Fork)

`gemini-seo` is the Antigravity-native fork of [AgriciDaniel/claude-seo](https://github.com/AgriciDaniel/claude-seo).

## Branch Strategy

- `main`: Clean mirror of upstream `AgriciDaniel/claude-seo` (never edited directly, so upstream syncs merge cleanly).
- `antigravity`: Contains the Antigravity conversion tooling (`tools/sync_antigravity.py`), generated Antigravity manifest and hook configs (`plugin.json`, `hooks.json`), Antigravity-adapted `skills/` and `agents/`, `assets/logo.svg`, `rules/seo-runtime.md`, and Antigravity integration tests (`tests/test_antigravity_plugin.py`).

## What `tools/sync_antigravity.py` Converts

- `plugin.json`: Generated from `.claude-plugin/plugin.json` using the official `https://antigravity.google/schemas/v1/plugin.json` schema (`name: "gemini-seo"`, `displayName: "Gemini SEO"`, `logo: "assets/logo.svg"`, `suggestedPrompts`, and preserved skill/agent counts).
- `hooks.json`: Generated from `hooks/hooks.json` for Antigravity's `PostToolUse` schema (`validate-schema` named group matching file edit/write tools, running `node ./tools/antigravity-schema-hook.mjs`, which bridges Antigravity stdin JSON payloads to `./hooks/run-python-hook.js` and `./hooks/validate-schema.py`).
- `agents/*.md`: Converts Claude Code frontmatter (`model`, `tools`, `maxTurns`) into Antigravity agent frontmatter (`model: inherit|pro|flash`, `mainAgent: false`, `subagent: true`, native `tools` list, `inheritMcp`) and rewrites `${CLAUDE_PLUGIN_ROOT}` references.
- `skills/*/SKILL.md` and non-locked `skills/*/references/*.md`: Rewrites `${CLAUDE_PLUGIN_ROOT}` to `${GEMINI_SEO_ROOT:-$HOME/.gemini/config/plugins/gemini-seo}` and adapts `skills/seo/SKILL.md` invocation/runtime instructions while keeping `skills/seo-flow/references/prompts/*` untouched.

## Syncing Upstream Changes

```bash
git fetch upstream
git checkout main && git merge --ff-only upstream/main
git checkout antigravity
python3 tools/sync_antigravity.py --sync-from main
python3 -m unittest discover -s tests -p "test_antigravity_*.py" -v
agy plugin validate .
git commit -m "chore: sync upstream claude-seo changes"
```

## Validating and Installing

Check for conversion drift without editing files:

```bash
python3 tools/sync_antigravity.py --check
```

Run Antigravity's validator against a local checkout:

```bash
agy plugin validate .
```

When you are ready to install locally:

```bash
agy plugin install .
```

Optional extensions in `extensions/` remain intact from upstream and can be wired into `mcp_config.json` as needed.
