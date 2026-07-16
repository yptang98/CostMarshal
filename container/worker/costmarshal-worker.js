#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { spawn } = require("child_process");

const MAX_PROMPT_BYTES = 2 * 1024 * 1024;
const MAX_CREDENTIAL_BYTES = 256 * 1024;
const MAX_PROFILE_BYTES = 256 * 1024;
const ENV_KEY = /^[A-Z_][A-Z0-9_]{0,127}$/;

function fail(message, exitCode = 64) {
  const output = process.env.COSTMARSHAL_OUTPUT_PATH;
  if (output && path.resolve(output) === "/out/final.md") {
    try {
      fs.writeFileSync(output, `# Completion Report\n\nStatus: failed\n\n## Result\n${message}\n`, {
        encoding: "utf8",
        flag: "wx",
        mode: 0o600,
      });
    } catch (_) {
      // The host treats a missing or malformed exchange as a separate failure.
    }
  }
  process.stderr.write(`costmarshal-worker: ${message}\n`);
  process.exit(exitCode);
}

function parseArgs(argv) {
  let model = null;
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--jsonl") continue;
    if (value === "--model" && index + 1 < argv.length) {
      model = argv[index + 1];
      index += 1;
      continue;
    }
    fail("invalid worker argument");
  }
  return { model };
}

function fixedPath(envName, expected) {
  const value = process.env[envName];
  if (!value || path.resolve(value) !== expected) fail(`${envName} is invalid`);
  return value;
}

async function readPrompt() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_PROMPT_BYTES) fail("stdin prompt exceeds 2 MiB", 65);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function main() {
  const { model } = parseArgs(process.argv.slice(2));
  const profile = fixedPath("COSTMARSHAL_PROFILE_PATH", "/bootstrap/profile.config.toml");
  const output = fixedPath("COSTMARSHAL_OUTPUT_PATH", "/out/final.md");
  const workspaceMode = process.env.COSTMARSHAL_WORKSPACE_MODE;
  if (!new Set(["ro", "rw"]).has(workspaceMode)) fail("workspace mode is invalid");
  if (fs.existsSync(output)) fail("output exchange was not empty");

  const codexHome = "/home/worker/.codex";
  fs.mkdirSync(codexHome, { recursive: true, mode: 0o700 });
  const profileFd = fs.openSync(profile, "r");
  let profileBytes;
  try {
    const before = fs.fstatSync(profileFd, { bigint: true });
    if (!before.isFile() || before.size > BigInt(MAX_PROFILE_BYTES)) fail("provider profile is invalid");
    profileBytes = fs.readFileSync(profileFd);
    const after = fs.fstatSync(profileFd, { bigint: true });
    if (
      before.dev !== after.dev || before.ino !== after.ino || before.size !== after.size ||
      before.mtimeNs !== after.mtimeNs || before.ctimeNs !== after.ctimeNs
    ) fail("provider profile changed while being read");
  } finally {
    fs.closeSync(profileFd);
  }
  const expectedProfileSha = process.env.COSTMARSHAL_PROFILE_SHA256;
  if (!/^[0-9a-f]{64}$/.test(expectedProfileSha || "")) fail("provider profile identity is invalid");
  const observedProfileSha = crypto.createHash("sha256").update(profileBytes).digest("hex");
  if (observedProfileSha !== expectedProfileSha) fail("provider profile identity mismatch");
  const installedProfile = path.join(codexHome, "config.toml");
  fs.writeFileSync(installedProfile, profileBytes, { flag: "wx", mode: 0o600 });
  fs.chmodSync(installedProfile, 0o600);

  const childEnv = { CODEX_HOME: codexHome };
  for (const key of ["PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"]) {
    if (process.env[key]) childEnv[key] = process.env[key];
  }
  const secretFile = process.env.COSTMARSHAL_PROVIDER_SECRET_FILE;
  const providerEnvKey = process.env.COSTMARSHAL_PROVIDER_ENV_KEY;
  if (secretFile || providerEnvKey) {
    if (path.resolve(secretFile || "") !== "/run/secrets/provider" || !ENV_KEY.test(providerEnvKey || "")) {
      fail("provider credential contract is invalid");
    }
    const info = fs.statSync(secretFile);
    if (!info.isFile() || info.size > MAX_CREDENTIAL_BYTES) fail("provider credential is invalid");
    childEnv[providerEnvKey] = fs.readFileSync(secretFile, "utf8").trim();
    if (!childEnv[providerEnvKey]) fail("provider credential is empty");
  }

  const prompt = await readPrompt();
  const args = [
    "--ask-for-approval",
    "never",
    "exec",
    "--ephemeral",
    "--skip-git-repo-check",
    "--sandbox",
    workspaceMode === "rw" ? "workspace-write" : "read-only",
    "--cd",
    "/workspace",
    "--json",
    "--output-last-message",
    output,
  ];
  if (model) args.push("--model", model);
  args.push("-");

  const child = spawn("codex", args, {
    env: childEnv,
    cwd: "/workspace",
    stdio: ["pipe", "pipe", "pipe"],
    shell: false,
  });
  child.stdout.pipe(process.stdout);
  child.stderr.pipe(process.stderr);
  child.stdin.end(prompt, "utf8");
  const exitCode = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (code, signal) => resolve(code === null ? (signal ? 128 : 127) : code));
  }).catch(() => 127);

  if (!fs.existsSync(output)) {
    fail(`codex exited ${exitCode} without final.md`, exitCode || 70);
  }
  process.exit(exitCode);
}

main().catch(() => fail("worker bootstrap failed", 70));
