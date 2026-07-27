#!/usr/bin/env python3
"""Run the CostMarshal production credential broker or provider proxy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.production_gateway import (
    GatewayError,
    GatewayPolicy,
    serve_gateway,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CostMarshal mTLS credential broker and hard-budget provider proxy"
    )
    parser.add_argument("service", choices=("broker", "proxy", "validate-policy"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--signing-key-file", type=Path)
    parser.add_argument("--cert-file", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument(
        "--client-ca",
        type=Path,
        help="Client CA used by the broker; required for the broker service",
    )
    parser.add_argument("--audit-log", type=Path)
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.service == "validate-policy":
        try:
            policy = GatewayPolicy.load(args.config)
        except GatewayError as exc:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "code": exc.code,
                        "message": str(exc),
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        print(
            json.dumps(
                {
                    "status": "ok",
                    "policy_sha256": policy.sha256,
                    "providers": sorted(policy.providers),
                    "workloads": sorted(policy.workloads),
                    "database_path": str(policy.database_path),
                },
                sort_keys=True,
            )
        )
        return 0
    missing = [
        flag
        for flag, value in (
            ("--signing-key-file", args.signing_key_file),
            ("--cert-file", args.cert_file),
            ("--key-file", args.key_file),
        )
        if value is None
    ]
    if missing:
        build_parser().error(
            f"{', '.join(missing)} required for {args.service}"
        )
    if args.service == "broker" and args.client_ca is None:
        build_parser().error("--client-ca is required for the broker service")
    port = int(args.port or (8443 if args.service == "broker" else 9443))
    if port < 1 or port > 65535:
        build_parser().error("--port must be between 1 and 65535")
    try:
        serve_gateway(
            service=args.service,
            policy_path=args.config,
            signing_key_path=args.signing_key_file,
            cert_file=args.cert_file,
            key_file=args.key_file,
            client_ca_file=args.client_ca,
            audit_log=args.audit_log,
            listen_host=str(args.listen),
            listen_port=port,
        )
    except GatewayError as exc:
        print(
            json.dumps(
                {"status": "error", "code": exc.code, "message": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
