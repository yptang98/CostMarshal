#!/usr/bin/env python3
"""Collect live mTLS Broker/Proxy and real-provider production evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.production_gateway import (  # noqa: E402
    GatewayError,
    probe_gateway_health,
    request_provider_lease,
)


SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}\Z")
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def git_sha() -> str | None:
    try:
        value = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def safe_endpoint(value: str, *, suffix: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != suffix
    ):
        raise ValueError(f"endpoint must be a credential-free HTTPS {suffix} URL")
    return parsed.geturl()


def proxy_url(base: str, path: str) -> str:
    parsed = urllib.parse.urlsplit(base)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, "", "")
    )


def response_usage(payload: bytes) -> tuple[int | None, int | None]:
    try:
        row = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    usage = row.get("usage") if isinstance(row, dict) else None
    if not isinstance(usage, dict):
        return None, None
    raw_input = usage.get("input_tokens", usage.get("prompt_tokens"))
    raw_output = usage.get("output_tokens", usage.get("completion_tokens"))
    input_tokens = raw_input if type(raw_input) is int and raw_input >= 0 else None
    output_tokens = raw_output if type(raw_output) is int and raw_output >= 0 else None
    return input_tokens, output_tokens


def provider_payload(
    *,
    path: str,
    model: str,
    max_output_tokens: int,
) -> bytes:
    if path == "/v1/responses":
        row = {
            "model": model,
            "input": (
                "Production gateway canary. Reply with exactly "
                "COSTMARSHAL_GATEWAY_CANARY_OK."
            ),
            "max_output_tokens": max_output_tokens,
            "stream": False,
        }
    else:
        row = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Production gateway canary. Reply with exactly "
                        "COSTMARSHAL_GATEWAY_CANARY_OK."
                    ),
                }
            ],
            "max_tokens": max_output_tokens,
            "stream": False,
        }
    return json.dumps(
        row,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def proxy_request(
    *,
    base: str,
    path: str,
    lease_token: str,
    request_id: str,
    payload: bytes,
    ca_file: Path,
    timeout_seconds: float,
) -> tuple[int, dict[str, str], bytes]:
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=str(ca_file),
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    request = urllib.request.Request(
        proxy_url(base, path),
        data=payload,
        headers={
            "Authorization": f"Bearer {lease_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-CostMarshal-Request-Id": request_id,
        },
        method="POST",
    )
    try:
        response = urllib.request.urlopen(  # noqa: S310
            request,
            context=context,
            timeout=timeout_seconds,
        )
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeError("gateway evidence response exceeded 64 MiB")
        status = int(getattr(response, "status", 0))
        headers = {key.lower(): value for key, value in response.headers.items()}
    return status, headers, body


def error_code(payload: bytes) -> str | None:
    try:
        row = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(row, dict) or not isinstance(row.get("error"), dict):
        return None
    value = row["error"].get("code")
    return str(value) if isinstance(value, str) else None


def base_report() -> dict[str, Any]:
    return {
        "schema_version": "costmarshal-gateway-live-evidence-v1",
        "generated_at": utc_now(),
        "git_sha": git_sha(),
        "status": "blocked",
        "real_provider_call": False,
        "checks": [],
        "blockers": [],
    }


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    report = base_report()

    def record(name: str, passed: bool, detail: Any = None) -> None:
        report["checks"].append(
            {
                "name": name,
                "status": "pass" if passed else "fail",
                "detail": detail,
            }
        )

    try:
        broker_endpoint = safe_endpoint(args.broker_endpoint, suffix="/v1/leases")
        proxy_endpoint = safe_endpoint(args.proxy_endpoint, suffix="/v1")
        if not SHA256_RE.fullmatch(args.policy_sha256):
            raise ValueError("--policy-sha256 must use sha256:<64 lowercase hex>")
        if not NAME_RE.fullmatch(args.provider) or not NAME_RE.fullmatch(args.model):
            raise ValueError("provider and model identifiers are invalid")
        for label, path in (
            ("client certificate", args.client_cert),
            ("client private key", args.client_key),
            ("CA bundle", args.ca),
        ):
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"{label} must be an existing non-symlink file")
        if (
            args.budget_nano_cny <= 0
            or args.max_input_tokens <= 0
            or args.max_output_tokens <= 0
        ):
            raise ValueError("budget and token envelopes must be positive")
    except ValueError as exc:
        report["blockers"].append(str(exc))
        return 2, report

    report.update(
        {
            "policy_sha256": args.policy_sha256,
            "provider": args.provider,
            "model": args.model,
            "broker_endpoint_sha256": sha256_bytes(
                broker_endpoint.encode("utf-8")
            ),
            "proxy_endpoint_sha256": sha256_bytes(
                proxy_endpoint.encode("utf-8")
            ),
            "request_path": args.path,
        }
    )
    try:
        broker_health = probe_gateway_health(
            endpoint=broker_endpoint,
            expected_policy_sha256=args.policy_sha256,
            ca_file=args.ca,
            client_cert_file=args.client_cert,
            client_key_file=args.client_key,
            timeout_seconds=args.timeout_seconds,
        )
        proxy_health = probe_gateway_health(
            endpoint=proxy_endpoint,
            expected_policy_sha256=args.policy_sha256,
            ca_file=args.ca,
            timeout_seconds=args.timeout_seconds,
        )
        record("broker_health_policy_bound", broker_health.get("status") == "pass")
        record("proxy_health_policy_bound", proxy_health.get("status") == "pass")

        idempotency_key = "gateway-evidence-" + uuid.uuid4().hex
        lease_args = {
            "endpoint": broker_endpoint,
            "client_cert_file": args.client_cert,
            "client_key_file": args.client_key,
            "ca_file": args.ca,
            "provider": args.provider,
            "model": args.model,
            "budget_nano_cny": args.budget_nano_cny,
            "max_input_tokens": args.max_input_tokens,
            "max_output_tokens": args.max_output_tokens,
            "idempotency_key": idempotency_key,
            "ttl_seconds": args.ttl_seconds,
            "timeout_seconds": args.timeout_seconds,
        }
        first_lease = request_provider_lease(**lease_args)
        replayed_lease = request_provider_lease(**lease_args)
        same_lease = (
            first_lease["lease_id"] == replayed_lease["lease_id"]
            and first_lease["lease_token"] == replayed_lease["lease_token"]
            and first_lease.get("replayed") is False
            and replayed_lease.get("replayed") is True
        )
        record("broker_idempotent_attempt_lease", same_lease)

        request_id = "gateway-evidence-" + uuid.uuid4().hex
        payload = provider_payload(
            path=args.path,
            model=args.model,
            max_output_tokens=args.max_output_tokens,
        )
        status, headers, body = proxy_request(
            base=proxy_endpoint,
            path=args.path,
            lease_token=first_lease["lease_token"],
            request_id=request_id,
            payload=payload,
            ca_file=args.ca,
            timeout_seconds=args.timeout_seconds,
        )
        input_tokens, output_tokens = response_usage(body)
        settlement = headers.get("x-costmarshal-settlement")
        report.update(
            {
                "real_provider_call": True,
                "provider_http_status": status,
                "provider_response_sha256": sha256_bytes(body),
                "provider_response_bytes": len(body),
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
                "settlement": settlement,
            }
        )
        record(
            "real_provider_success",
            HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES,
            status,
        )
        record(
            "provider_usage_reported",
            input_tokens is not None and output_tokens is not None,
        )
        record("proxy_settlement", settlement == "settled", settlement)

        replay_status, _, replay_body = proxy_request(
            base=proxy_endpoint,
            path=args.path,
            lease_token=first_lease["lease_token"],
            request_id=request_id,
            payload=payload,
            ca_file=args.ca,
            timeout_seconds=args.timeout_seconds,
        )
        record(
            "proxy_request_replay_rejected",
            replay_status == HTTPStatus.CONFLICT
            and error_code(replay_body) == "request_replayed",
            {
                "status": replay_status,
                "error_code": error_code(replay_body),
            },
        )

        oversized_payload = provider_payload(
            path=args.path,
            model=args.model,
            max_output_tokens=args.max_output_tokens + 1,
        )
        cap_status, _, cap_body = proxy_request(
            base=proxy_endpoint,
            path=args.path,
            lease_token=first_lease["lease_token"],
            request_id="gateway-evidence-" + uuid.uuid4().hex,
            payload=oversized_payload,
            ca_file=args.ca,
            timeout_seconds=args.timeout_seconds,
        )
        record(
            "lease_output_cap_rejected_before_provider",
            cap_status == HTTPStatus.FORBIDDEN
            and error_code(cap_body) == "output_cap_exceeded",
            {
                "status": cap_status,
                "error_code": error_code(cap_body),
            },
        )
    except (GatewayError, OSError, RuntimeError, urllib.error.URLError) as exc:
        report["status"] = "fail"
        report["blockers"].append(
            f"live gateway evidence failed safely: {type(exc).__name__}"
        )
        return 1, report

    passed = all(item["status"] == "pass" for item in report["checks"])
    report["status"] = "pass" if passed else "fail"
    return (0 if passed else 1), report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Collect deployment-specific CostMarshal gateway evidence"
    )
    value.add_argument(
        "--broker-endpoint",
        default=os.environ.get("COSTMARSHAL_BROKER_ENDPOINT", ""),
    )
    value.add_argument(
        "--proxy-endpoint",
        default=os.environ.get("COSTMARSHAL_PROVIDER_PROXY_ENDPOINT", ""),
    )
    value.add_argument(
        "--policy-sha256",
        default=os.environ.get("COSTMARSHAL_GATEWAY_POLICY_SHA256", ""),
    )
    value.add_argument(
        "--client-cert",
        type=Path,
        default=Path(os.environ.get("COSTMARSHAL_BROKER_CLIENT_CERT_FILE", "")),
    )
    value.add_argument(
        "--client-key",
        type=Path,
        default=Path(os.environ.get("COSTMARSHAL_BROKER_CLIENT_KEY_FILE", "")),
    )
    value.add_argument(
        "--ca",
        type=Path,
        default=Path(os.environ.get("COSTMARSHAL_BROKER_CA_FILE", "")),
    )
    value.add_argument(
        "--provider",
        default=os.environ.get("COSTMARSHAL_GATEWAY_EVIDENCE_PROVIDER", ""),
    )
    value.add_argument(
        "--model",
        default=os.environ.get("COSTMARSHAL_GATEWAY_EVIDENCE_MODEL", ""),
    )
    value.add_argument(
        "--path",
        choices=["/v1/responses", "/v1/chat/completions"],
        default="/v1/responses",
    )
    value.add_argument(
        "--budget-nano-cny",
        type=int,
        default=int(os.environ.get("COSTMARSHAL_GATEWAY_EVIDENCE_BUDGET_NANO_CNY", "0")),
    )
    value.add_argument("--max-input-tokens", type=int, default=2048)
    value.add_argument("--max-output-tokens", type=int, default=64)
    value.add_argument("--ttl-seconds", type=int, default=300)
    value.add_argument("--timeout-seconds", type=float, default=60.0)
    value.add_argument(
        "--artifact",
        type=Path,
        default=ROOT / "artifacts" / "gateway-runtime-report.json",
    )
    return value


def main() -> int:
    args = parser().parse_args()
    code, report = run(args)
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "artifact": str(args.artifact.resolve()),
                "checks": len(report["checks"]),
                "git_sha": report["git_sha"],
            },
            sort_keys=True,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
