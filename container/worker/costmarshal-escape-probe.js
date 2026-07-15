#!/usr/bin/env node
"use strict";

// This command is test-only. It deliberately behaves like a hostile worker and
// reports booleans only; it never prints credential contents or host paths.
const fs = require("fs");
const net = require("net");
const crypto = require("crypto");
const http = require("http");
const https = require("https");

const MODES = new Set(["boundary", "symlink-output", "extra-output", "oversize-output"]);

function parseArgs(argv) {
  let mode = null;
  let holdMs = 0;
  let proxyHealthUrl = null;
  let proxyHealthSha256 = null;
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--mode" && index + 1 < argv.length) {
      mode = argv[index + 1];
      index += 1;
    } else if (argv[index] === "--hold-ms" && index + 1 < argv.length) {
      holdMs = Number(argv[index + 1]);
      index += 1;
    } else if (argv[index] === "--proxy-health-url" && index + 1 < argv.length) {
      proxyHealthUrl = argv[index + 1];
      index += 1;
    } else if (argv[index] === "--proxy-health-sha256" && index + 1 < argv.length) {
      proxyHealthSha256 = argv[index + 1];
      index += 1;
    } else {
      throw new Error("invalid probe argument");
    }
  }
  if (!MODES.has(mode) || !Number.isInteger(holdMs) || holdMs < 0 || holdMs > 10000) {
    throw new Error("invalid probe configuration");
  }
  if ((proxyHealthUrl === null) !== (proxyHealthSha256 === null)) throw new Error("incomplete proxy health configuration");
  if (proxyHealthSha256 !== null && !/^[0-9a-f]{64}$/.test(proxyHealthSha256)) throw new Error("invalid proxy health digest");
  return { mode, holdMs, proxyHealthUrl, proxyHealthSha256 };
}

function existsAny(paths) {
  return paths.some((candidate) => {
    try {
      return fs.existsSync(candidate);
    } catch (_) {
      return false;
    }
  });
}

function writeProbe(target) {
  try {
    fs.writeFileSync(target, "probe", { encoding: "utf8", flag: "wx" });
    fs.unlinkSync(target);
    return true;
  } catch (_) {
    return false;
  }
}

function externalTcpReachable() {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: "1.1.1.1", port: 53 });
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(value);
    };
    socket.setTimeout(750, () => finish(false));
    socket.once("connect", () => finish(true));
    socket.once("error", () => finish(false));
  });
}

function proxyHealthVerified(rawUrl, expectedSha256) {
  if (rawUrl === null) return Promise.resolve(null);
  return new Promise((resolve) => {
    let parsed;
    try {
      parsed = new URL(rawUrl);
    } catch (_) {
      resolve(false);
      return;
    }
    const client = parsed.protocol === "https:" ? https : parsed.protocol === "http:" ? http : null;
    if (client === null || parsed.username || parsed.password || parsed.hash) {
      resolve(false);
      return;
    }
    const request = client.get(parsed, { timeout: 2000 }, (response) => {
      const chunks = [];
      let size = 0;
      response.on("data", (chunk) => {
        size += chunk.length;
        if (size > 64 * 1024) request.destroy();
        else chunks.push(chunk);
      });
      response.on("end", () => {
        const digest = crypto.createHash("sha256").update(Buffer.concat(chunks)).digest("hex");
        resolve(response.statusCode >= 200 && response.statusCode < 300 && digest === expectedSha256);
      });
    });
    request.once("timeout", () => request.destroy());
    request.once("error", () => resolve(false));
  });
}

function createOutputAttack(mode) {
  if (mode === "boundary") {
    fs.writeFileSync("/out/final.md", "# Probe\n\nStatus: done\n", { encoding: "utf8", flag: "wx" });
    return true;
  }
  if (mode === "symlink-output") {
    fs.symlinkSync("/workspace/task.txt", "/out/final.md");
    return true;
  }
  if (mode === "extra-output") {
    fs.writeFileSync("/out/final.md", "# Probe\n", { encoding: "utf8", flag: "wx" });
    fs.writeFileSync("/out/extra.txt", "escape", { encoding: "utf8", flag: "wx" });
    return true;
  }
  fs.writeFileSync("/out/final.md", Buffer.alloc(1024 * 1024 + 1, 0x78), { flag: "wx" });
  return true;
}

async function main() {
  const { mode, holdMs, proxyHealthUrl, proxyHealthSha256 } = parseArgs(process.argv.slice(2));
  const uid = process.getuid ? process.getuid() : -1;
  const runtimeVisible = existsAny(["/runtime", "/costmarshal-runtime", "/app/runtime"]);
  const aggregateSecretsVisible = existsAny([
    "/run/secrets/providers.env",
    "/run/secrets/aggregate",
    "/host-secrets",
    "/secrets.env",
  ]);
  const engineSocketVisible = existsAny([
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/podman/podman.sock",
    "/var/run/podman/podman.sock",
    `/run/user/${uid}/podman/podman.sock`,
    "/run/containerd/containerd.sock",
  ]);
  const aggregateSecretEnvVisible = Object.keys(process.env).some(
    (key) => /(?:API_KEY|TOKEN|SECRET|PASSWORD)$/.test(key) && !key.startsWith("COSTMARSHAL_"),
  );
  const credentialPath = "/run/secrets/provider";
  let selectedCredentialVisible = false;
  try {
    selectedCredentialVisible = fs.statSync(credentialPath).isFile();
  } catch (_) {
    selectedCredentialVisible = false;
  }
  const workspaceWritable = writeProbe(`/workspace/.costmarshal-escape-${process.pid}`);
  const externalNetworkReachable = await externalTcpReachable();
  const proxyHealth = await proxyHealthVerified(proxyHealthUrl, proxyHealthSha256);
  let outputAttackCreated = false;
  try {
    outputAttackCreated = createOutputAttack(mode);
  } catch (_) {
    outputAttackCreated = false;
  }
  process.stdout.write(
    `${JSON.stringify({
      type: "costmarshal_escape_probe",
      mode,
      runtime_visible: runtimeVisible,
      aggregate_secrets_visible: aggregateSecretsVisible || aggregateSecretEnvVisible,
      engine_socket_visible: engineSocketVisible,
      selected_credential_visible: selectedCredentialVisible,
      workspace_writable: workspaceWritable,
      external_network_reachable: externalNetworkReachable,
      proxy_health_verified: proxyHealth,
      output_attack_created: outputAttackCreated,
    })}\n`,
  );
  if (holdMs) await new Promise((resolve) => setTimeout(resolve, holdMs));
}

main().catch(() => process.exit(70));
