#!/usr/bin/env python3
"""Probe the live gateway for a secret-free provider schema-drift receipt."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RELEASE_TESTS = Path(__file__).resolve().parent
for entry in (ROOT, RELEASE_TESTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from costmarshal_v2.production_gateway import (  # noqa: E402
    GatewayError,
    probe_gateway_health,
    request_provider_lease,
)
from run_gateway_evidence import (  # noqa: E402
    NAME_RE,
    SHA256_RE,
    git_sha,
    provider_payload,
    proxy_request,
    response_usage,
    safe_endpoint,
    sha256_bytes,
    utc_now,
)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _schema_receipt(row: dict[str, Any]) -> dict[str, Any]:
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    output = row.get("output") if isinstance(row.get("output"), list) else []
    output_shapes: list[dict[str, Any]] = []
    for item in output[:8]:
        if not isinstance(item, dict):
            output_shapes.append({"type": _type_name(item)})
            continue
        content = item.get("content") if isinstance(item.get("content"), list) else []
        output_shapes.append(
            {
                "keys": sorted(item),
                "field_types": {
                    key: _type_name(item[key]) for key in sorted(item)
                },
                "content_item_types": sorted(
                    {
                        str(part.get("type"))
                        for part in content
                        if isinstance(part, dict) and isinstance(part.get("type"), str)
                    }
                ),
            }
        )
    receipt = {
        "top_level_keys": sorted(row),
        "top_level_types": {
            key: _type_name(row[key]) for key in sorted(row)
        },
        "usage_keys": sorted(usage),
        "usage_types": {
            key: _type_name(usage[key]) for key in sorted(usage)
        },
        "output_shapes": output_shapes,
    }
    encoded = json.dumps(
        receipt,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    receipt["schema_sha256"] = sha256_bytes(encoded)
    return receipt


def _base_report() -> dict[str, Any]:
    return {
        "schema_version": "costmarshal-provider-schema-drift-v1",
        "generated_at": utc_now(),
        "git_sha": git_sha(),
        "status": "blocked",
        "real_provider_call": False,
        "checks": [],
        "blockers": [],
    }


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    report = _base_report()

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
            "request_path": "/v1/responses",
        }
    )
    try:
        health = probe_gateway_health(
            endpoint=proxy_endpoint,
            expected_policy_sha256=args.policy_sha256,
            ca_file=args.ca,
            timeout_seconds=args.timeout_seconds,
        )
        record("proxy_health_policy_bound", health.get("status") == "pass")
        lease = request_provider_lease(
            endpoint=broker_endpoint,
            client_cert_file=args.client_cert,
            client_key_file=args.client_key,
            ca_file=args.ca,
            provider=args.provider,
            model=args.model,
            budget_nano_cny=args.budget_nano_cny,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
            idempotency_key="schema-drift-" + uuid.uuid4().hex,
            ttl_seconds=args.ttl_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        body_payload = provider_payload(
            path="/v1/responses",
            model=args.model,
            max_output_tokens=args.max_output_tokens,
        )
        status, headers, body = proxy_request(
            base=proxy_endpoint,
            path="/v1/responses",
            lease_token=lease["lease_token"],
            request_id="schema-drift-" + uuid.uuid4().hex,
            payload=body_payload,
            ca_file=args.ca,
            timeout_seconds=args.timeout_seconds,
        )
        report["real_provider_call"] = True
        report["provider_http_status"] = status
        report["provider_response_sha256"] = sha256_bytes(body)
        report["provider_response_bytes"] = len(body)
        record(
            "responses_http_contract",
            HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES,
            status,
        )
        content_type = headers.get("content-type", "").split(";", 1)[0].lower()
        record("responses_content_type", content_type == "application/json")
        try:
            row = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            row = None
        record("responses_json_object", isinstance(row, dict))
        if isinstance(row, dict):
            input_tokens, output_tokens = response_usage(body)
            output = row.get("output")
            record(
                "responses_usage_contract",
                input_tokens is not None and output_tokens is not None,
            )
            record(
                "responses_output_contract",
                isinstance(output, list)
                and all(isinstance(item, dict) for item in output),
            )
            report["schema"] = _schema_receipt(row)
        else:
            record("responses_usage_contract", False)
            record("responses_output_contract", False)
    except (GatewayError, OSError, RuntimeError) as exc:
        report["status"] = "fail"
        report["blockers"].append(
            f"provider drift probe failed safely: {type(exc).__name__}"
        )
        return 1, report

    passed = all(item["status"] == "pass" for item in report["checks"])
    report["status"] = "pass" if passed else "fail"
    return (0 if passed else 1), report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Collect live, secret-free provider schema drift evidence"
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
        default=os.environ.get("COSTMARSHAL_SCHEMA_DRIFT_PROVIDER", ""),
    )
    value.add_argument(
        "--model",
        default=os.environ.get("COSTMARSHAL_SCHEMA_DRIFT_MODEL", ""),
    )
    value.add_argument(
        "--budget-nano-cny",
        type=int,
        default=int(os.environ.get("COSTMARSHAL_SCHEMA_DRIFT_BUDGET_NANO_CNY", "0")),
    )
    value.add_argument("--max-input-tokens", type=int, default=2048)
    value.add_argument("--max-output-tokens", type=int, default=64)
    value.add_argument("--ttl-seconds", type=int, default=300)
    value.add_argument("--timeout-seconds", type=float, default=60.0)
    value.add_argument(
        "--artifact",
        type=Path,
        default=ROOT / "artifacts" / "provider-schema-drift-report.json",
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
