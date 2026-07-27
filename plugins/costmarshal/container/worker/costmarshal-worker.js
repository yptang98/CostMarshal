#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { spawn } = require("child_process");

const MAX_PROMPT_BYTES = 2 * 1024 * 1024;
const MAX_CREDENTIAL_BYTES = 256 * 1024;
const MAX_PROFILE_BYTES = 256 * 1024;
const MAX_IMAGE_BYTES = 16 * 1024 * 1024;
const MAX_MULTIMODAL_BYTES = 2 * 1024 * 1024;
const MAX_REQUEST_BYTES = 4 * 1024 * 1024;
const MAX_RESPONSE_BYTES = 64 * 1024 * 1024;
const ENV_KEY = /^[A-Z_][A-Z0-9_]{0,127}$/;
const IMAGE_SUFFIXES = new Set([".gif", ".jpeg", ".jpg", ".png", ".webp"]);
const ATTACHMENT_SUFFIXES = {
  image: IMAGE_SUFFIXES,
  audio: new Set([".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".webm"]),
  video: new Set([".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm"]),
  document: new Set([".csv", ".doc", ".docx", ".html", ".json", ".md", ".pdf", ".pptx", ".rtf", ".txt", ".xlsx"]),
};

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
  let mode = "agent";
  let maxOutputTokens = null;
  const attachments = { image: [], audio: [], video: [], document: [] };
  let totalAttachmentBytes = 0;
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--jsonl") continue;
    if (value === "--mode" && index + 1 < argv.length) {
      mode = argv[index + 1];
      if (!new Set(["agent", "multimodal-api"]).has(mode)) fail("invalid worker execution mode");
      index += 1;
      continue;
    }
    if (value === "--max-output-tokens" && index + 1 < argv.length) {
      maxOutputTokens = Number(argv[index + 1]);
      if (!Number.isSafeInteger(maxOutputTokens) || maxOutputTokens <= 0 || maxOutputTokens > 1000000) {
        fail("invalid worker output-token envelope");
      }
      index += 1;
      continue;
    }
    if (value === "--model" && index + 1 < argv.length) {
      model = argv[index + 1];
      index += 1;
      continue;
    }
    const modality = value.startsWith("--") ? value.slice(2) : "";
    if (Object.hasOwn(ATTACHMENT_SUFFIXES, modality) && index + 1 < argv.length) {
      const attachment = path.resolve(argv[index + 1]);
      const workspacePrefix = `${path.sep}workspace${path.sep}`;
      if (
        !attachment.startsWith(workspacePrefix) ||
        !ATTACHMENT_SUFFIXES[modality].has(path.extname(attachment).toLowerCase())
      ) fail(`invalid worker ${modality} input`);
      const info = fs.statSync(attachment);
      const real = fs.realpathSync(attachment);
      const maximum = modality === "image" && mode === "agent" ? MAX_IMAGE_BYTES : MAX_MULTIMODAL_BYTES;
      if (
        !info.isFile() ||
        info.size > maximum ||
        !real.startsWith(workspacePrefix)
      ) fail(`invalid worker ${modality} input`);
      attachments[modality].push(attachment);
      totalAttachmentBytes += info.size;
      index += 1;
      continue;
    }
    fail("invalid worker argument");
  }
  if (mode === "agent" && (attachments.audio.length || attachments.video.length || attachments.document.length)) {
    fail("agent mode cannot transport non-image attachments");
  }
  if (mode === "multimodal-api" && totalAttachmentBytes > MAX_MULTIMODAL_BYTES) {
    fail("multimodal attachment envelope exceeds 2 MiB");
  }
  if (mode === "multimodal-api" && maxOutputTokens === null) {
    fail("multimodal-api requires an output-token envelope");
  }
  return { model, mode, maxOutputTokens, attachments };
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

function tomlString(profileText, key) {
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const matches = [...profileText.matchAll(new RegExp(`^${escaped}\\s*=\\s*(\"(?:[^\"\\\\]|\\\\.)*\")\\s*$`, "gm"))];
  if (matches.length !== 1) fail(`provider profile ${key} is missing or ambiguous`);
  try {
    const value = JSON.parse(matches[0][1]);
    if (typeof value !== "string" || !value) fail(`provider profile ${key} is invalid`);
    return value;
  } catch (_) {
    fail(`provider profile ${key} is invalid`);
  }
}

function attachmentMediaType(modality, attachmentPath) {
  const suffix = path.extname(attachmentPath).toLowerCase();
  const fixed = {
    ".aac": "audio/aac",
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".flac": "audio/flac",
    ".gif": "image/gif",
    ".html": "text/html",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".m4a": "audio/mp4",
    ".md": "text/markdown",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".wav": "audio/wav",
    ".webp": "image/webp",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  };
  if (suffix === ".webm") return modality === "audio" ? "audio/webm" : "video/webm";
  return fixed[suffix] || "application/octet-stream";
}

function dataUrl(modality, attachmentPath) {
  const payload = fs.readFileSync(attachmentPath);
  return `data:${attachmentMediaType(modality, attachmentPath)};base64,${payload.toString("base64")}`;
}

function responseText(row) {
  if (typeof row.output_text === "string" && row.output_text) return row.output_text;
  const texts = [];
  if (Array.isArray(row.output)) {
    for (const item of row.output) {
      if (!item || typeof item !== "object" || !Array.isArray(item.content)) continue;
      for (const part of item.content) {
        if (!part || typeof part !== "object") continue;
        if (typeof part.text === "string" && part.text) texts.push(part.text);
        else if (typeof part.output_text === "string" && part.output_text) texts.push(part.output_text);
      }
    }
  }
  return texts.join("\n").trim();
}

async function invokeMultimodalApi({ profileText, providerSecret, providerEnvKey, model, maxOutputTokens, attachments, prompt }) {
  if (!providerSecret) fail("multimodal-api requires a scoped provider lease");
  const profileEnvKey = tomlString(profileText, "env_key");
  if (profileEnvKey !== providerEnvKey) fail("provider profile credential binding mismatch");
  if (tomlString(profileText, "wire_api") !== "responses") {
    fail("multimodal-api requires a Responses worker profile");
  }
  const profileModel = tomlString(profileText, "model");
  const selectedModel = model || profileModel;
  if (selectedModel !== profileModel) fail("worker model override drifted from the profile");
  let endpoint;
  try {
    const base = new URL(tomlString(profileText, "base_url"));
    if (
      base.protocol !== "https:" ||
      base.username ||
      base.password ||
      base.search ||
      base.hash
    ) fail("multimodal-api profile endpoint is invalid");
    endpoint = new URL(`${base.pathname.replace(/\/+$/, "")}/responses`, base.origin);
  } catch (_) {
    fail("multimodal-api profile endpoint is invalid");
  }

  const content = [{ type: "input_text", text: prompt }];
  for (const image of attachments.image) {
    content.push({ type: "input_image", image_url: dataUrl("image", image) });
  }
  for (const audio of attachments.audio) {
    content.push({
      type: "input_audio",
      input_audio: {
        data: fs.readFileSync(audio).toString("base64"),
        format: path.extname(audio).slice(1).toLowerCase(),
      },
    });
  }
  for (const video of attachments.video) {
    content.push({ type: "input_video", video_url: dataUrl("video", video) });
  }
  for (const document of attachments.document) {
    content.push({
      type: "input_file",
      file_data: dataUrl("document", document),
      filename: path.basename(document),
    });
  }
  const requestPayload = Buffer.from(JSON.stringify({
    model: selectedModel,
    input: [{ type: "message", role: "user", content }],
    max_output_tokens: maxOutputTokens,
    stream: false,
    store: false,
  }), "utf8");
  if (requestPayload.length > MAX_REQUEST_BYTES) fail("multimodal-api request exceeds 4 MiB");

  const https = require("https");
  const response = await new Promise((resolve, reject) => {
    const request = https.request(endpoint, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${providerSecret}`,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Content-Length": String(requestPayload.length),
        "X-CostMarshal-Request-Id": `worker-${crypto.randomUUID()}`,
      },
      timeout: 120000,
    }, (incoming) => {
      const chunks = [];
      let size = 0;
      incoming.on("data", (chunk) => {
        size += chunk.length;
        if (size > MAX_RESPONSE_BYTES) {
          incoming.destroy(new Error("response too large"));
          return;
        }
        chunks.push(chunk);
      });
      incoming.on("end", () => resolve({
        statusCode: incoming.statusCode || 0,
        headers: incoming.headers,
        body: Buffer.concat(chunks),
      }));
      incoming.on("error", reject);
    });
    request.on("timeout", () => request.destroy(new Error("request timeout")));
    request.on("error", reject);
    request.end(requestPayload);
  }).catch(() => fail("multimodal-api request failed", 70));
  if (response.statusCode < 200 || response.statusCode >= 300) {
    fail(`multimodal-api proxy returned HTTP ${response.statusCode}`, 70);
  }
  if (response.headers["x-costmarshal-settlement"] !== "settled") {
    fail("multimodal-api proxy did not settle the request", 70);
  }
  let row;
  try {
    row = JSON.parse(response.body.toString("utf8"));
  } catch (_) {
    fail("multimodal-api response is not JSON", 70);
  }
  const usage = row && typeof row.usage === "object" ? row.usage : null;
  const inputTokens = usage && Number.isSafeInteger(usage.input_tokens) && usage.input_tokens >= 0
    ? usage.input_tokens : null;
  const outputTokens = usage && Number.isSafeInteger(usage.output_tokens) && usage.output_tokens >= 0
    ? usage.output_tokens : null;
  if (inputTokens === null || outputTokens === null) {
    fail("multimodal-api response lacks authoritative usage", 70);
  }
  const text = responseText(row);
  if (!text) fail("multimodal-api response contains no text output", 70);
  return {
    text,
    usage: { input_tokens: inputTokens, output_tokens: outputTokens },
    responseSha256: crypto.createHash("sha256").update(response.body).digest("hex"),
  };
}

async function main() {
  const { model, mode, maxOutputTokens, attachments } = parseArgs(process.argv.slice(2));
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
  let providerSecret = null;
  if (secretFile || providerEnvKey) {
    if (path.resolve(secretFile || "") !== "/run/secrets/provider" || !ENV_KEY.test(providerEnvKey || "")) {
      fail("provider credential contract is invalid");
    }
    const info = fs.statSync(secretFile);
    if (!info.isFile() || info.size > MAX_CREDENTIAL_BYTES) fail("provider credential is invalid");
    providerSecret = fs.readFileSync(secretFile, "utf8").trim();
    if (!providerSecret) fail("provider credential is empty");
    childEnv[providerEnvKey] = providerSecret;
  }

  const prompt = await readPrompt();
  if (mode === "multimodal-api") {
    const result = await invokeMultimodalApi({
      profileText: profileBytes.toString("utf8"),
      providerSecret,
      providerEnvKey,
      model,
      maxOutputTokens,
      attachments,
      prompt,
    });
    const report = [
      "# Completion Report",
      "",
      "Status: done",
      "",
      "## Result",
      result.text,
      "",
      "## Evidence",
      `- Provider response SHA-256: sha256:${result.responseSha256}`,
      `- Usage: ${result.usage.input_tokens} input / ${result.usage.output_tokens} output tokens`,
      "- Gateway settlement: settled",
      "",
      "## Blockers",
      "- None.",
      "",
    ].join("\n");
    fs.writeFileSync(output, report, { encoding: "utf8", flag: "wx", mode: 0o600 });
    process.stdout.write(`${JSON.stringify({
      type: "costmarshal.multimodal.completed",
      usage: result.usage,
      response_sha256: `sha256:${result.responseSha256}`,
      settlement: "settled",
    })}\n`);
    process.exit(0);
  }
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
  for (const image of attachments.image) args.push("--image", image);
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

module.exports = {
  attachmentMediaType,
  invokeMultimodalApi,
  responseText,
};

if (require.main === module) {
  main().catch(() => fail("worker bootstrap failed", 70));
}
