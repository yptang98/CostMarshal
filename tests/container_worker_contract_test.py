#!/usr/bin/env python3
"""Static release contract for the digest-pinned CostMarshal worker image."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GITATTRIBUTES = ROOT / ".gitattributes"
WORKER = ROOT / "container" / "worker" / "costmarshal-worker.js"
CANARY = ROOT / "container" / "worker" / "costmarshal-isolation-canary.js"
PROBE = ROOT / "container" / "worker" / "costmarshal-escape-probe.js"
DOCKERFILE = ROOT / "container" / "worker" / "Dockerfile"
DOCKERIGNORE = ROOT / "container" / "worker" / ".dockerignore"
LIVE_HARNESS = ROOT / "tests" / "oci_live_evidence.py"


def main() -> int:
    for script in (WORKER, CANARY, PROBE):
        raw = script.read_bytes()
        assert raw.startswith(b"#!/usr/bin/env node\n")
        assert b"\r" not in raw
        completed = subprocess.run(
            ["node", "--check", str(script)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
    module_contract = subprocess.run(
        [
            "node",
            "-e",
            (
                "const w=require(process.argv[1]);"
                "if(w.attachmentMediaType('audio','/workspace/a.webm')!=='audio/webm')process.exit(2);"
                "if(w.attachmentMediaType('video','/workspace/a.webm')!=='video/webm')process.exit(3);"
                "const text=w.responseText({output:[{content:[{type:'output_text',text:'ok'}]}]});"
                "if(text!=='ok')process.exit(4);"
            ),
            str(WORKER),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert module_contract.returncode == 0, module_contract.stderr
    with tempfile.TemporaryDirectory(prefix="costmarshal-worker-test-") as temporary:
        audio_path = Path(temporary) / "sample.wav"
        audio_path.write_bytes(b"RIFF-costmarshal-audio")
        live_module_contract = subprocess.run(
            [
                "node",
                "-e",
                (
                    "const {EventEmitter}=require('events');"
                    "const https=require('https');"
                    "const w=require(process.argv[1]);"
                    "let captured=null;"
                    "https.request=(endpoint,options,callback)=>{"
                    "const request=new EventEmitter();"
                    "request.destroy=(error)=>request.emit('error',error);"
                    "request.end=(body)=>{captured={endpoint:String(endpoint),options,body:JSON.parse(body)};"
                    "const incoming=new EventEmitter();"
                    "incoming.statusCode=200;"
                    "incoming.headers={'x-costmarshal-settlement':'settled'};"
                    "incoming.destroy=(error)=>incoming.emit('error',error);"
                    "callback(incoming);"
                    "process.nextTick(()=>{"
                    "incoming.emit('data',Buffer.from(JSON.stringify({"
                    "output_text:'accepted',usage:{input_tokens:7,output_tokens:3}})));"
                    "incoming.emit('end');});};"
                    "return request;};"
                    "(async()=>{const result=await w.invokeMultimodalApi({"
                    "profileText:'model = \"mimo-v2-flash\"\\nbase_url = \"https://gateway.example/v1\"\\n"
                    "wire_api = \"responses\"\\nenv_key = \"MIMO_API_KEY\"\\n',"
                    "providerSecret:'scoped-lease',providerEnvKey:'MIMO_API_KEY',"
                    "model:'mimo-v2-flash',maxOutputTokens:32,"
                    "attachments:{image:[],audio:[process.argv[2]],video:[],document:[]},"
                    "prompt:'inspect audio'});"
                    "if(captured.endpoint!=='https://gateway.example/v1/responses')process.exit(11);"
                    "if(captured.options.headers.Authorization!=='Bearer scoped-lease')process.exit(12);"
                    "if(captured.body.input[0].content[1].type!=='input_audio')process.exit(13);"
                    "if(result.text!=='accepted'||result.usage.input_tokens!==7||"
                    "result.usage.output_tokens!==3)process.exit(14);"
                    "process.stdout.write(JSON.stringify({ok:true,responseSha256:result.responseSha256}));"
                    "})().catch(()=>process.exit(15));"
                ),
                str(WORKER),
                str(audio_path),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert live_module_contract.returncode == 0, live_module_contract.stderr
        live_result = json.loads(live_module_contract.stdout)
        assert live_result["ok"] is True
        assert len(live_result["responseSha256"]) == 64
    worker = WORKER.read_text(encoding="utf-8")
    canary = CANARY.read_text(encoding="utf-8")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8")
    attributes = GITATTRIBUTES.read_text(encoding="utf-8")
    assert b"\r" not in DOCKERFILE.read_bytes()
    assert b"\r" not in DOCKERIGNORE.read_bytes()
    assert dockerignore.splitlines() == [
        "*",
        "!Dockerfile",
        "!costmarshal-worker.js",
        "!costmarshal-isolation-canary.js",
        "!costmarshal-escape-probe.js",
    ]
    for required in (
        "container/worker/*.js text eol=lf",
        "container/worker/Dockerfile text eol=lf",
        "container/worker/.dockerignore text eol=lf",
        "plugins/costmarshal/container/worker/*.js text eol=lf",
        "plugins/costmarshal/container/worker/Dockerfile text eol=lf",
        "plugins/costmarshal/container/worker/.dockerignore text eol=lf",
    ):
        assert required in attributes
    for required in (
        'path.resolve(value) !== expected',
        '"--ask-for-approval"',
        '"--ephemeral"',
        '"--skip-git-repo-check"',
        '"--json"',
        '"--output-last-message"',
        '"--image"',
        '"multimodal-api"',
        '"--max-output-tokens"',
        "input_audio",
        "input_video",
        "input_file",
        "X-CostMarshal-Request-Id",
        "x-costmarshal-settlement",
        "response_sha256",
        "workspacePrefix",
        "MAX_IMAGE_BYTES",
        'shell: false',
        'fs.existsSync(output)',
    ):
        assert required in worker
    assert "eval(" not in worker and "exec(" not in worker
    assert "provider-secret" not in worker
    for required in (
        "no_new_privileges",
        "rootfs_write_blocked",
        "workspace_writable",
        "runtime_visible",
        "aggregate_secrets_visible",
        "engine_socket_visible",
    ):
        assert required in canary
    assert "ARG NODE_BASE_IMAGE" in dockerfile
    assert "# syntax=" not in dockerfile
    assert "FROM ${NODE_BASE_IMAGE}" in dockerfile
    assert "ARG CODEX_NPM_VERSION" in dockerfile
    assert "COPY costmarshal-escape-probe.js" in dockerfile
    assert '@openai/codex@${CODEX_NPM_VERSION}' in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "ENTRYPOINT []" in dockerfile
    assert ":latest" not in dockerfile
    harness = LIVE_HARNESS.read_text(encoding="utf-8")
    for required in (
        "artifacts\" / \"oci-attestation.json",
        "mount_allowlist_excludes_runtime_and_aggregate",
        "symlink-output",
        "extra-output",
        "oversize-output",
        "credential_cleanup",
        "network_policy",
    ):
        assert required in harness
    print("container worker contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
