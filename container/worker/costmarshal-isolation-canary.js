#!/usr/bin/env node
"use strict";

const fs = require("fs");

function writeProbe(target) {
  try {
    fs.writeFileSync(target, "probe", { encoding: "utf8", flag: "wx" });
    fs.unlinkSync(target);
    return true;
  } catch (_) {
    return false;
  }
}

const status = fs.readFileSync("/proc/self/status", "utf8");
const uid = Number((status.match(/^Uid:\s+(\d+)/m) || [null, "0"])[1]);
const capEff = (status.match(/^CapEff:\s+([0-9a-fA-F]+)/m) || [null, "unknown"])[1];
const noNewPrivileges = (status.match(/^NoNewPrivs:\s+(\d+)/m) || [null, "0"])[1] === "1";
const workspaceProbe = `/workspace/.costmarshal-canary-${process.pid}`;
const outputProbe = `/out/.costmarshal-canary-${process.pid}`;
const aggregateSecretPaths = [
  "/run/secrets/providers.env",
  "/run/secrets/aggregate",
  "/host-secrets",
  "/secrets.env",
];
const engineSocketPaths = [
  "/var/run/docker.sock",
  "/run/docker.sock",
  "/run/podman/podman.sock",
  "/var/run/podman/podman.sock",
  `/run/user/${uid}/podman/podman.sock`,
  "/run/containerd/containerd.sock",
];

const payload = {
  schema: "costmarshal-worker-isolation-canary-v1",
  uid,
  cap_eff: capEff,
  no_new_privileges: noNewPrivileges,
  rootfs_write_blocked: !writeProbe(`/.costmarshal-canary-${process.pid}`),
  workspace_readable: fs.existsSync("/workspace") && fs.statSync("/workspace").isDirectory(),
  workspace_writable: writeProbe(workspaceProbe),
  output_writable: writeProbe(outputProbe),
  runtime_visible: fs.existsSync("/runtime") || fs.existsSync("/costmarshal-runtime"),
  aggregate_secrets_visible: aggregateSecretPaths.some((candidate) => fs.existsSync(candidate)),
  engine_socket_visible: engineSocketPaths.some((candidate) => fs.existsSync(candidate)),
};

process.stdout.write(`${JSON.stringify(payload)}\n`);
