#!/usr/bin/env python3
"""Production broker/proxy security, budget, and recovery contracts."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.production_gateway import (
    AuditLog,
    BrokerCore,
    GatewayError,
    GatewayLedger,
    GatewayPolicy,
    LeaseSigner,
    ProviderProxyCore,
    build_gateway,
    probe_gateway_health,
    workload_identity_from_peer,
)


IDENTITY = "spiffe://example.test/costmarshal/worker"
KEY = b"k" * 32


def policy_mapping(database: Path) -> dict:
    return {
        "schema_version": "costmarshal-production-gateway-policy-v1",
        "issuer": "costmarshal-broker",
        "audience": "costmarshal-provider-proxy",
        "lease_ttl_seconds": 300,
        "database_path": str(database),
        "providers": {
            "provider-a": {
                "base_url": "https://provider.example/v1",
                "credential_env": "PROVIDER_A_API_KEY",
                "auth_header": "Authorization",
                "auth_scheme": "Bearer",
                "models": {
                    "model-a": {
                        "input_nano_cny_per_million": 1_000_000,
                        "output_nano_cny_per_million": 1_000_000,
                        "request_overhead_tokens": 10,
                        "max_output_tokens": 1000,
                    }
                },
            }
        },
        "workloads": {
            IDENTITY: {
                "providers": ["provider-a"],
                "max_lease_budget_nano_cny": 1_000_000,
                "max_lease_ttl_seconds": 300,
            }
        },
    }


class ProductionGatewayContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy = GatewayPolicy.from_mapping(
            policy_mapping(self.root / "gateway.db")
        )
        self.signer = LeaseSigner(
            KEY,
            issuer=self.policy.issuer,
            audience=self.policy.audience,
        )
        self.ledger = GatewayLedger(self.policy.database_path)
        self.audit_path = self.root / "audit.jsonl"
        self.audit = AuditLog(self.audit_path)
        self.broker = BrokerCore(
            self.policy,
            self.signer,
            self.ledger,
            self.audit,
        )
        self.proxy = ProviderProxyCore(
            self.policy,
            self.signer,
            self.ledger,
            self.audit,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def issue(
        self,
        *,
        budget: int = 10_000,
        key: str = "lease-key-0001",
        max_input_tokens: int = 1_000,
        max_output_tokens: int = 100,
    ) -> dict:
        return self.broker.issue(
            IDENTITY,
            {
                "provider": "provider-a",
                "model": "model-a",
                "budget_nano_cny": budget,
                "max_input_tokens": max_input_tokens,
                "max_output_tokens": max_output_tokens,
                "ttl_seconds": 120,
                "idempotency_key": key,
            },
            now=1_700_000_000,
        )

    def prepare(
        self,
        token: str,
        request_id: str,
        *,
        max_tokens: int = 100,
        now: int = 1_700_000_001,
    ):
        return self.proxy.prepare(
            token=token,
            request_id=request_id,
            path="/v1/responses",
            payload=json.dumps(
                {"model": "model-a", "max_output_tokens": max_tokens, "input": "hello"}
            ).encode(),
            now=now,
        )

    def test_policy_is_secret_free_and_strict(self) -> None:
        raw = policy_mapping(self.root / "strict.db")
        raw["providers"]["provider-a"]["base_url"] = "https://user:secret@provider.example/v1"
        with self.assertRaisesRegex(GatewayError, "credential-free"):
            GatewayPolicy.from_mapping(raw)

        raw = policy_mapping(self.root / "strict.db")
        raw["providers"]["provider-a"]["api_key"] = "secret"
        with self.assertRaisesRegex(GatewayError, "provider provider-a is invalid"):
            GatewayPolicy.from_mapping(raw)

        raw = policy_mapping(self.root / "strict.db")
        raw["workloads"] = {"worker-name": raw["workloads"][IDENTITY]}
        with self.assertRaisesRegex(GatewayError, "SPIFFE"):
            GatewayPolicy.from_mapping(raw)

        credential_file = self.root / "provider.secret"
        credential_file.write_text("file-secret", encoding="utf-8")
        raw = policy_mapping(self.root / "file.db")
        provider = raw["providers"]["provider-a"]
        provider.pop("credential_env")
        provider["credential_file"] = str(credential_file)
        file_policy = GatewayPolicy.from_mapping(raw)
        self.assertEqual(
            file_policy.providers["provider-a"].credential_file,
            credential_file.resolve(),
        )

        raw = policy_mapping(self.root / "wire.db")
        provider = raw["providers"]["provider-a"]
        provider["wire_api"] = "chat-completions"
        provider["input_modalities"] = ["text", "video"]
        with self.assertRaisesRegex(GatewayError, "cannot transport"):
            GatewayPolicy.from_mapping(raw)

    def test_mtls_identity_requires_one_spiffe_uri(self) -> None:
        peer = SimpleNamespace(
            getpeercert=lambda: {
                "subjectAltName": (("DNS", "worker.example"), ("URI", IDENTITY))
            }
        )
        handler = SimpleNamespace(connection=peer)
        self.assertEqual(workload_identity_from_peer(handler), IDENTITY)

        duplicate = SimpleNamespace(
            connection=SimpleNamespace(
                getpeercert=lambda: {
                    "subjectAltName": (
                        ("URI", IDENTITY),
                        ("URI", "spiffe://example.test/other"),
                    )
                }
            )
        )
        with self.assertRaisesRegex(GatewayError, "exactly one"):
            workload_identity_from_peer(duplicate)

    def test_lease_is_scoped_signed_short_lived_and_idempotent(self) -> None:
        first = self.issue()
        second = self.broker.issue(
            IDENTITY,
            {
                "provider": "provider-a",
                "model": "model-a",
                "budget_nano_cny": 10_000,
                "max_input_tokens": 1_000,
                "max_output_tokens": 100,
                "ttl_seconds": 120,
                "idempotency_key": "lease-key-0001",
            },
            now=1_700_000_050,
        )
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["lease_token"], second["lease_token"])
        claims = self.signer.verify(first["lease_token"], now=1_700_000_001)
        self.assertEqual(claims["provider"], "provider-a")
        self.assertEqual(claims["model"], "model-a")
        self.assertEqual(claims["budget_nano_cny"], 10_000)
        self.assertNotIn("PROVIDER_A_API_KEY", json.dumps(claims))

        with self.assertRaisesRegex(GatewayError, "different input"):
            self.broker.issue(
                IDENTITY,
                {
                    "provider": "provider-a",
                    "model": "model-a",
                    "budget_nano_cny": 9_999,
                    "max_input_tokens": 1_000,
                    "max_output_tokens": 100,
                    "ttl_seconds": 120,
                    "idempotency_key": "lease-key-0001",
                },
                now=1_700_000_050,
            )
        with self.assertRaisesRegex(GatewayError, "expired"):
            self.signer.verify(first["lease_token"], now=1_700_000_121)

    def test_proxy_enforces_model_path_output_cap_and_token_signature(self) -> None:
        lease = self.issue()
        with self.assertRaisesRegex(GatewayError, "does not match"):
            self.proxy.prepare(
                token=lease["lease_token"],
                request_id="request-00000001",
                path="/v1/responses",
                payload=b'{"model":"other","max_output_tokens":10}',
                now=1_700_000_001,
            )
        prepared = self.proxy.prepare(
            token=lease["lease_token"],
            request_id="request-00000002",
            path="/v1/responses",
            payload=b'{"model":"model-a","input":"hello"}',
            now=1_700_000_001,
        )
        self.assertIn(b'"max_output_tokens":100', prepared.payload)
        with self.assertRaisesRegex(GatewayError, "not allowlisted"):
            self.proxy.prepare(
                token=lease["lease_token"],
                request_id="request-00000003",
                path="/v1/files",
                payload=b'{"model":"model-a","max_output_tokens":10}',
                now=1_700_000_001,
            )
        token_parts = lease["lease_token"].split(".")
        token_parts[2] = (
            ("A" if token_parts[2][0] != "A" else "B")
            + token_parts[2][1:]
        )
        forged = ".".join(token_parts)
        with self.assertRaisesRegex(GatewayError, "signature"):
            self.prepare(forged, "request-00000004")

    def test_chat_provider_adapts_responses_text_image_tools_and_stream(self) -> None:
        raw = policy_mapping(self.root / "chat-adapter.db")
        raw_provider = raw["providers"]["provider-a"]
        raw_provider["wire_api"] = "chat-completions"
        raw_provider["input_modalities"] = ["text", "image", "audio"]
        policy = GatewayPolicy.from_mapping(raw)
        signer = LeaseSigner(KEY, issuer=policy.issuer, audience=policy.audience)
        ledger = GatewayLedger(policy.database_path)
        broker = BrokerCore(policy, signer, ledger, self.audit)
        proxy = ProviderProxyCore(policy, signer, ledger, self.audit)
        lease = broker.issue(
            IDENTITY,
            {
                "provider": "provider-a",
                "model": "model-a",
                "budget_nano_cny": 100_000,
                "max_input_tokens": 20_000,
                "max_output_tokens": 100,
                "ttl_seconds": 120,
                "idempotency_key": "chat-adapter-lease",
            },
            now=1_700_000_000,
        )
        request = {
            "model": "model-a",
            "instructions": "Be concise.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Describe this."},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,iVBORw0KGgo=",
                            "detail": "low",
                        },
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": "UklGRg==",
                                "format": "wav",
                            },
                        },
                    ],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Look up one value",
                    "parameters": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                    },
                }
            ],
            "tool_choice": "auto",
            "max_output_tokens": 50,
            "stream": True,
            "store": False,
        }
        prepared = proxy.prepare(
            token=lease["lease_token"],
            request_id="request-chat-adapter-0001",
            path="/v1/responses",
            payload=json.dumps(request).encode(),
            now=1_700_000_001,
        )
        self.assertEqual(prepared.upstream_path, "/v1/chat/completions")
        self.assertEqual(prepared.response_adapter, "chat-to-responses-v1")
        upstream = json.loads(prepared.upstream_payload)
        self.assertFalse(upstream["stream"])
        self.assertEqual(upstream["max_tokens"], 50)
        self.assertEqual(upstream["messages"][0]["role"], "system")
        self.assertEqual(
            upstream["messages"][1]["content"][1]["type"],
            "image_url",
        )
        self.assertEqual(
            upstream["messages"][1]["content"][2],
            {
                "type": "input_audio",
                "input_audio": {"data": "UklGRg==", "format": "wav"},
            },
        )
        self.assertEqual(upstream["tools"][0]["function"]["name"], "lookup")

        chat_response = json.dumps(
            {
                "id": "chatcmpl-test",
                "created": 1_700_000_002,
                "model": "model-a",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "COSTMARSHAL_GATEWAY_CANARY_OK",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 4,
                    "total_tokens": 24,
                },
            }
        ).encode()
        adapted, content_type, streaming = proxy.adapt_response(
            prepared,
            status=200,
            content_type="application/json",
            payload=chat_response,
        )
        self.assertTrue(streaming)
        self.assertEqual(content_type, "text/event-stream")
        self.assertIn(b"event: response.output_text.delta", adapted)
        self.assertIn(b"event: response.completed", adapted)
        self.assertIn(b"COSTMARSHAL_GATEWAY_CANARY_OK", adapted)
        _, input_tokens, output_tokens = proxy.actual_cost(
            prepared,
            chat_response,
        )
        self.assertEqual((input_tokens, output_tokens), (20, 4))

        request["input"][0]["content"] = [
            {"type": "input_file", "file_url": "https://example.test/doc.pdf"}
        ]
        with self.assertRaisesRegex(GatewayError, "modalities"):
            proxy.prepare(
                token=lease["lease_token"],
                request_id="request-chat-adapter-0002",
                path="/v1/responses",
                payload=json.dumps(request).encode(),
                now=1_700_000_001,
            )

    def test_http_proxy_emits_responses_sse_for_chat_upstream(self) -> None:
        raw = policy_mapping(self.root / "chat-http.db")
        raw_provider = raw["providers"]["provider-a"]
        raw_provider["wire_api"] = "chat-completions"
        raw_provider["input_modalities"] = ["text"]
        policy = GatewayPolicy.from_mapping(raw)
        signer = LeaseSigner(KEY, issuer=policy.issuer, audience=policy.audience)
        ledger = GatewayLedger(policy.database_path)
        broker = BrokerCore(policy, signer, ledger, self.audit)
        lease = broker.issue(
            IDENTITY,
            {
                "provider": "provider-a",
                "model": "model-a",
                "budget_nano_cny": 100_000,
                "max_input_tokens": 10_000,
                "max_output_tokens": 100,
                "ttl_seconds": 120,
                "idempotency_key": "chat-http-lease",
            },
            now=int(time.time()),
        )
        upstream_body = json.dumps(
            {
                "id": "chatcmpl-http",
                "created": int(time.time()),
                "model": "model-a",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "translated",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                },
            }
        ).encode()

        class FakeUpstream:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __init__(self, payload: bytes) -> None:
                self.stream = io.BytesIO(payload)

            def read(self, size: int = -1) -> bytes:
                return self.stream.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        server = build_gateway(
            service="proxy",
            policy=policy,
            signing_key=KEY,
            audit_log=self.root / "http-audit.jsonl",
            listen_host="127.0.0.1",
            listen_port=0,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        connection: http.client.HTTPConnection | None = None
        with patch.dict(os.environ, {"PROVIDER_A_API_KEY": "proxy-secret"}):
            with patch(
                "costmarshal_v2.production_gateway.urllib.request.urlopen",
                return_value=FakeUpstream(upstream_body),
            ):
                thread.start()
                try:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1",
                        server.server_address[1],
                        timeout=10,
                    )
                    payload = json.dumps(
                        {
                            "model": "model-a",
                            "input": "hello",
                            "max_output_tokens": 20,
                            "stream": True,
                            "store": False,
                        }
                    ).encode()
                    connection.request(
                        "POST",
                        "/v1/responses",
                        body=payload,
                        headers={
                            "Authorization": f"Bearer {lease['lease_token']}",
                            "Content-Type": "application/json",
                            "X-CostMarshal-Request-Id": "request-chat-http-0001",
                        },
                    )
                    response = connection.getresponse()
                    body = response.read()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        response.getheader("Content-Type"),
                        "text/event-stream",
                    )
                    self.assertIn(b"event: response.completed", body)
                    self.assertIn(b"translated", body)
                finally:
                    if connection is not None:
                        connection.close()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

    def test_atomic_budget_admission_and_request_replay(self) -> None:
        lease = self.issue(
            budget=800,
            key="lease-budget-001",
            max_input_tokens=200,
            max_output_tokens=100,
        )

        def attempt(index: int) -> str:
            try:
                self.prepare(
                    lease["lease_token"],
                    f"request-concurrent-{index:03d}",
                    max_tokens=100,
                )
                return "accepted"
            except GatewayError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(attempt, range(8)))
        self.assertIn("accepted", results)
        self.assertIn("budget_exhausted", results)

        accepted_index = results.index("accepted")
        with self.assertRaisesRegex(GatewayError, "will not be repeated"):
            self.prepare(
                lease["lease_token"],
                f"request-concurrent-{accepted_index:03d}",
                max_tokens=100,
            )

        with contextlib.closing(self.ledger._connect()) as connection:
            row = connection.execute(
                "SELECT budget_nano_cny, reserved_nano_cny, spent_nano_cny FROM leases"
            ).fetchone()
        self.assertLessEqual(
            row["reserved_nano_cny"] + row["spent_nano_cny"],
            row["budget_nano_cny"],
        )

    def test_settlement_releases_reservation_and_overrun_revokes_lease(self) -> None:
        lease = self.issue()
        prepared = self.prepare(lease["lease_token"], "request-settle-0001")
        self.ledger.mark_dispatched(prepared.request_id, now=1_700_000_002)
        response = json.dumps(
            {"usage": {"input_tokens": 10, "output_tokens": 20}}
        ).encode()
        actual, input_tokens, output_tokens = self.proxy.actual_cost(prepared, response)
        settled = self.ledger.settle(
            prepared.request_id,
            actual_nano_cny=actual,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            now=1_700_000_003,
        )
        self.assertEqual(actual, 30)
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(settled["lease"]["reserved_nano_cny"], 0)
        self.assertEqual(settled["lease"]["spent_nano_cny"], 30)

        second = self.prepare(lease["lease_token"], "request-settle-0002")
        self.ledger.mark_dispatched(second.request_id, now=1_700_000_004)
        overrun = self.ledger.settle(
            second.request_id,
            actual_nano_cny=second.reserved_nano_cny + 1,
            input_tokens=999,
            output_tokens=999,
            now=1_700_000_005,
        )
        self.assertEqual(overrun["state"], "overrun")
        self.assertEqual(overrun["lease"]["status"], "overrun")
        with self.assertRaisesRegex(GatewayError, "missing, expired, or revoked"):
            self.prepare(lease["lease_token"], "request-settle-0003")

    def test_provider_secret_stays_proxy_side_and_out_of_audit(self) -> None:
        lease = self.issue()
        prepared = self.prepare(lease["lease_token"], "request-secret-0001")
        secret = "provider-secret-value"
        old = os.environ.get("PROVIDER_A_API_KEY")
        os.environ["PROVIDER_A_API_KEY"] = secret
        try:
            header, value = self.proxy.credential_header(prepared)
        finally:
            if old is None:
                os.environ.pop("PROVIDER_A_API_KEY", None)
            else:
                os.environ["PROVIDER_A_API_KEY"] = old
        self.assertEqual(header, "Authorization")
        self.assertEqual(value, f"Bearer {secret}")
        audit_text = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, audit_text)
        self.assertNotIn(lease["lease_token"], audit_text)
        self.assertNotIn("PROVIDER_A_API_KEY", audit_text)

    def test_unknown_usage_is_charged_at_full_reservation(self) -> None:
        lease = self.issue()
        prepared = self.prepare(lease["lease_token"], "request-unknown-0001")
        actual, input_tokens, output_tokens = self.proxy.actual_cost(
            prepared,
            b'{"id":"response-without-usage"}',
        )
        self.assertEqual(actual, prepared.reserved_nano_cny)
        self.assertIsNone(input_tokens)
        self.assertIsNone(output_tokens)

    def test_streaming_sse_usage_is_settled_from_final_event(self) -> None:
        lease = self.issue()
        prepared = self.proxy.prepare(
            token=lease["lease_token"],
            request_id="request-stream-0001",
            path="/v1/responses",
            payload=json.dumps(
                {
                    "model": "model-a",
                    "max_output_tokens": 100,
                    "input": "hello",
                    "stream": True,
                }
            ).encode(),
            now=1_700_000_001,
        )
        self.assertTrue(prepared.streaming)
        sse = (
            b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
            b'data: {"type":"response.completed","response":{"usage":'
            b'{"input_tokens":12,"output_tokens":7}}}\n\n'
            b"data: [DONE]\n\n"
        )
        actual, input_tokens, output_tokens = self.proxy.actual_cost(
            prepared,
            sse,
        )
        self.assertEqual((input_tokens, output_tokens), (12, 7))
        self.assertEqual(actual, 19)

    def test_health_probe_is_tls_and_policy_hash_bound(self) -> None:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(
                    {
                        "schema_version": "costmarshal-production-gateway-health-v1",
                        "status": "ok",
                        "policy_sha256": self_policy,
                    }
                ).encode()

        self_policy = self.policy.sha256
        fake_context = SimpleNamespace(
            minimum_version=None,
            load_cert_chain=lambda *_args: None,
        )
        with patch(
            "costmarshal_v2.production_gateway.ssl.create_default_context",
            return_value=fake_context,
        ), patch(
            "costmarshal_v2.production_gateway.urllib.request.urlopen",
            return_value=Response(),
        ) as opened:
            result = probe_gateway_health(
                endpoint="https://proxy.example/v1",
                expected_policy_sha256=self_policy,
                ca_file=self.root / "ca.pem",
            )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            opened.call_args.args[0].full_url,
            "https://proxy.example/healthz",
        )


if __name__ == "__main__":
    unittest.main()
