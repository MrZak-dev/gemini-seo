#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCHEMA_FILE_EXTENSIONS = [
  ".html",
  ".htm",
  ".jsx",
  ".tsx",
  ".vue",
  ".svelte",
  ".php",
  ".ejs",
];

const ARG_FILE_KEYS = [
  "Path",
  "path",
  "TargetFile",
  "AbsolutePath",
  "FilePath",
  "file_path",
];

function writeHookResponse() {
  process.stdout.write("{}\n");
}

function readAllStdin() {
  try {
    return fs.readFileSync(0, "utf8");
  } catch {
    return "";
  }
}

function parseCliFileFlag(argv) {
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--file" && i + 1 < argv.length) {
      return argv[i + 1];
    }
    if (argv[i].startsWith("--file=")) {
      return argv[i].slice("--file=".length);
    }
  }
  return "";
}

function extractFilePath(payload) {
  if (!payload || typeof payload !== "object") {
    return "";
  }
  const args = payload.toolCall && typeof payload.toolCall === "object"
    ? payload.toolCall.args
    : null;
  if (args && typeof args === "object") {
    for (const key of ARG_FILE_KEYS) {
      const value = args[key];
      if (typeof value === "string" && value.trim()) {
        return value.trim();
      }
    }
  }
  if (payload.tool_input && typeof payload.tool_input === "object") {
    const fp = payload.tool_input.file_path;
    if (typeof fp === "string" && fp.trim()) {
      return fp.trim();
    }
  }
  return "";
}

function resolveTargetPath(rawPath, cwd) {
  if (!rawPath) {
    return "";
  }
  if (path.isAbsolute(rawPath)) {
    return path.normalize(rawPath);
  }
  if (typeof cwd === "string" && cwd.trim()) {
    return path.resolve(cwd.trim(), rawPath);
  }
  return path.resolve(rawPath);
}

function hasSchemaExtension(filePath) {
  const lower = filePath.toLowerCase();
  return SCHEMA_FILE_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

function forwardDiagnostics(stdoutText, stderrText) {
  const trimmedErr = (stderrText || "").trim();
  if (trimmedErr) {
    process.stderr.write(`${trimmedErr}\n`);
  }
  const trimmedOut = (stdoutText || "").trim();
  if (!trimmedOut) {
    return;
  }
  try {
    const parsed = JSON.parse(trimmedOut);
    const context = parsed?.hookSpecificOutput?.additionalContext;
    if (typeof context === "string" && context.trim()) {
      process.stderr.write(`${context.trim()}\n`);
      return;
    }
  } catch {
    // Non-JSON diagnostic output is forwarded to stderr below.
  }
  process.stderr.write(`${trimmedOut}\n`);
}

function main() {
  const cliFile = parseCliFileFlag(process.argv.slice(2));
  let rawPath = cliFile;
  let cwd = "";

  if (!rawPath) {
    const rawStdin = readAllStdin().trim();
    if (!rawStdin) {
      writeHookResponse();
      return;
    }
    let payload;
    try {
      payload = JSON.parse(rawStdin);
    } catch {
      writeHookResponse();
      return;
    }
    rawPath = extractFilePath(payload);
    if (typeof payload.cwd === "string") {
      cwd = payload.cwd;
    }
  }

  const resolvedPath = resolveTargetPath(rawPath, cwd);
  if (!resolvedPath || !hasSchemaExtension(resolvedPath)) {
    writeHookResponse();
    return;
  }

  try {
    if (!fs.statSync(resolvedPath).isFile()) {
      writeHookResponse();
      return;
    }
  } catch {
    writeHookResponse();
    return;
  }

  const toolsDir = path.dirname(fileURLToPath(import.meta.url));
  const pluginRoot = path.resolve(toolsDir, "..");
  const runnerPath = path.join(pluginRoot, "hooks", "run-python-hook.js");
  const validatorPath = path.join(pluginRoot, "hooks", "validate-schema.py");

  if (!fs.existsSync(runnerPath) || !fs.existsSync(validatorPath)) {
    writeHookResponse();
    return;
  }

  const result = spawnSync(
    process.execPath,
    [runnerPath, validatorPath, resolvedPath],
    {
      cwd: pluginRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  forwardDiagnostics(result.stdout, result.stderr);
  writeHookResponse();
}

main();
