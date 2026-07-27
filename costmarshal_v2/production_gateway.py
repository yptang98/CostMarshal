"""Production credential broker and hard-budget provider proxy.

The gateway is deliberately separate from scheduler state.  Workers receive a
short-lived, provider-scoped lease token; only the proxy process can read the
real provider credential.  A shared SQLite ledger makes lease issuance,
request admission, reservation, and settlement durable and atomic.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping


POLICY_SCHEMA = "costmarshal-production-gateway-policy-v1"
LEASE_SCHEMA = "costmarshal-provider-lease-v1"
HEALTH_SCHEMA = "costmarshal-production-gateway-health-v1"
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z")
_ALLOWED_PATHS = {"/v1/responses", "/v1/chat/completions"}
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_UPSTREAM_RESPONSE_BYTES = 64 * 1024 * 1024


class GatewayError(RuntimeError):
    """A fail-closed gateway error with a stable machine code."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical_bytes(value)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _b64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise GatewayError("lease_invalid", "lease token encoding is invalid", status=401) from exc
    if _b64url(decoded) != value:
        raise GatewayError("lease_invalid", "lease token encoding is non-canonical", status=401)
    return decoded


def _require_name(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_NAME.fullmatch(text):
        raise GatewayError("policy_invalid", f"{label} is invalid")
    return text


def _positive_int(value: Any, label: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise GatewayError("policy_invalid", f"{label} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise GatewayError("policy_invalid", f"{label} must be an integer") from exc
    if parsed <= 0 or (maximum is not None and parsed > maximum):
        raise GatewayError("policy_invalid", f"{label} is outside the allowed range")
    return parsed


def _nonnegative_int(
    value: Any,
    label: str,
    *,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool):
        raise GatewayError("policy_invalid", f"{label} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise GatewayError("policy_invalid", f"{label} must be an integer") from exc
    if parsed < 0 or (maximum is not None and parsed > maximum):
        raise GatewayError("policy_invalid", f"{label} is outside the allowed range")
    return parsed


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _token_cost(tokens: int, rate_nano_cny_per_million: int) -> int:
    return _ceil_div(tokens * rate_nano_cny_per_million, 1_000_000)


def _reservation_cost(
    price: "ModelPrice",
    *,
    input_tokens: int,
    output_tokens: int,
) -> int:
    base = (
        _token_cost(input_tokens, price.input_nano_cny_per_million)
        + _token_cost(output_tokens, price.output_nano_cny_per_million)
        + price.fixed_nano_cny_per_request
    )
    return _ceil_div(base * price.billing_safety_ppm, 1_000_000)


def _https_base_url(value: Any) -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise GatewayError(
            "policy_invalid",
            "provider base_url must be a credential-free HTTPS URL",
        )
    return parsed.geturl().rstrip("/")


@dataclass(frozen=True)
class ModelPrice:
    input_nano_cny_per_million: int
    output_nano_cny_per_million: int
    request_overhead_tokens: int
    max_output_tokens: int
    fixed_nano_cny_per_request: int
    billing_safety_ppm: int


@dataclass(frozen=True)
class ProviderPolicy:
    provider_id: str
    base_url: str
    credential_env: str | None
    credential_file: Path | None
    auth_header: str
    auth_scheme: str
    models: Mapping[str, ModelPrice]


@dataclass(frozen=True)
class WorkloadPolicy:
    identity: str
    providers: tuple[str, ...]
    max_lease_budget_nano_cny: int
    max_lease_ttl_seconds: int


@dataclass(frozen=True)
class GatewayPolicy:
    issuer: str
    audience: str
    lease_ttl_seconds: int
    database_path: Path
    providers: Mapping[str, ProviderPolicy]
    workloads: Mapping[str, WorkloadPolicy]
    sha256: str

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        config_dir: Path | None = None,
    ) -> "GatewayPolicy":
        row = json.loads(json.dumps(value))
        if row.get("schema_version") != POLICY_SCHEMA:
            raise GatewayError("policy_invalid", "gateway policy schema is invalid")
        if set(row) - {
            "schema_version",
            "issuer",
            "audience",
            "lease_ttl_seconds",
            "database_path",
            "providers",
            "workloads",
        }:
            raise GatewayError("policy_invalid", "gateway policy has unknown fields")
        issuer = _require_name(row.get("issuer"), "issuer")
        audience = _require_name(row.get("audience"), "audience")
        lease_ttl = _positive_int(
            row.get("lease_ttl_seconds"),
            "lease_ttl_seconds",
            maximum=3600,
        )
        raw_db = Path(str(row.get("database_path") or "")).expanduser()
        if not str(raw_db):
            raise GatewayError("policy_invalid", "database_path is required")
        database_path = (
            (config_dir / raw_db).resolve()
            if config_dir is not None and not raw_db.is_absolute()
            else raw_db.resolve()
        )
        providers = _parse_providers(
            row.get("providers"),
            config_dir=config_dir,
        )
        workloads = _parse_workloads(row.get("workloads"), providers, lease_ttl)
        return cls(
            issuer=issuer,
            audience=audience,
            lease_ttl_seconds=lease_ttl,
            database_path=database_path,
            providers=providers,
            workloads=workloads,
            sha256=_sha256(row),
        )

    @classmethod
    def load(cls, path: Path) -> "GatewayPolicy":
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise GatewayError("policy_unavailable", "gateway policy cannot be read") from exc
        if len(payload) > 1024 * 1024:
            raise GatewayError("policy_invalid", "gateway policy exceeds 1 MiB")
        try:
            row = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError("policy_invalid", "gateway policy is not valid UTF-8 JSON") from exc
        if not isinstance(row, dict):
            raise GatewayError("policy_invalid", "gateway policy must be an object")
        return cls.from_mapping(row, config_dir=path.resolve().parent)


def _parse_providers(
    value: Any,
    *,
    config_dir: Path | None,
) -> dict[str, ProviderPolicy]:
    if not isinstance(value, dict) or not value:
        raise GatewayError("policy_invalid", "providers must be a non-empty object")
    providers: dict[str, ProviderPolicy] = {}
    for provider_key, raw in value.items():
        provider_id = _require_name(provider_key, "provider id")
        if not isinstance(raw, dict) or set(raw) - {
            "base_url",
            "credential_env",
            "credential_file",
            "auth_header",
            "auth_scheme",
            "models",
        }:
            raise GatewayError("policy_invalid", f"provider {provider_id} is invalid")
        credential_env = (
            str(raw.get("credential_env") or "").strip() or None
        )
        credential_file_raw = (
            str(raw.get("credential_file") or "").strip() or None
        )
        if (credential_env is None) == (credential_file_raw is None):
            raise GatewayError(
                "policy_invalid",
                f"provider {provider_id} requires exactly one credential source",
            )
        if credential_env is not None and not re.fullmatch(
            r"[A-Z][A-Z0-9_]{2,127}",
            credential_env,
        ):
            raise GatewayError(
                "policy_invalid",
                f"provider {provider_id} credential_env is invalid",
            )
        credential_file: Path | None = None
        if credential_file_raw is not None:
            candidate = Path(credential_file_raw).expanduser()
            credential_file = (
                (config_dir / candidate).resolve()
                if config_dir is not None and not candidate.is_absolute()
                else candidate.resolve()
            )
        auth_header = str(raw.get("auth_header") or "Authorization").strip()
        if auth_header.lower() not in {"authorization", "x-api-key", "api-key"}:
            raise GatewayError("policy_invalid", f"provider {provider_id} auth_header is unsupported")
        auth_scheme = str(raw.get("auth_scheme") or "Bearer").strip()
        if "\r" in auth_scheme or "\n" in auth_scheme or len(auth_scheme) > 32:
            raise GatewayError("policy_invalid", f"provider {provider_id} auth_scheme is invalid")
        raw_models = raw.get("models")
        if not isinstance(raw_models, dict) or not raw_models:
            raise GatewayError("policy_invalid", f"provider {provider_id} models are required")
        models: dict[str, ModelPrice] = {}
        for model_key, model_raw in raw_models.items():
            model_id = _require_name(model_key, "model id")
            if not isinstance(model_raw, dict) or set(model_raw) - {
                "input_nano_cny_per_million",
                "output_nano_cny_per_million",
                "request_overhead_tokens",
                "max_output_tokens",
                "fixed_nano_cny_per_request",
                "billing_safety_ppm",
            }:
                raise GatewayError("policy_invalid", f"model {provider_id}/{model_id} is invalid")
            models[model_id] = ModelPrice(
                input_nano_cny_per_million=_positive_int(
                    model_raw.get("input_nano_cny_per_million"),
                    "input price",
                ),
                output_nano_cny_per_million=_positive_int(
                    model_raw.get("output_nano_cny_per_million"),
                    "output price",
                ),
                request_overhead_tokens=_positive_int(
                    model_raw.get("request_overhead_tokens", 1024),
                    "request overhead",
                    maximum=1_000_000,
                ),
                max_output_tokens=_positive_int(
                    model_raw.get("max_output_tokens"),
                    "max output tokens",
                    maximum=10_000_000,
                ),
                fixed_nano_cny_per_request=_nonnegative_int(
                    model_raw.get("fixed_nano_cny_per_request", 0),
                    "fixed request price",
                ),
                billing_safety_ppm=_positive_int(
                    model_raw.get("billing_safety_ppm", 1_000_000),
                    "billing safety ppm",
                    maximum=10_000_000,
                ),
            )
            if models[model_id].billing_safety_ppm < 1_000_000:
                raise GatewayError(
                    "policy_invalid",
                    "billing safety ppm cannot be below 1000000",
                )
        providers[provider_id] = ProviderPolicy(
            provider_id=provider_id,
            base_url=_https_base_url(raw.get("base_url")),
            credential_env=credential_env,
            credential_file=credential_file,
            auth_header=auth_header,
            auth_scheme=auth_scheme,
            models=models,
        )
    return providers


def _parse_workloads(
    value: Any,
    providers: Mapping[str, ProviderPolicy],
    default_ttl: int,
) -> dict[str, WorkloadPolicy]:
    if not isinstance(value, dict) or not value:
        raise GatewayError("policy_invalid", "workloads must be a non-empty object")
    workloads: dict[str, WorkloadPolicy] = {}
    for identity, raw in value.items():
        identity_text = str(identity or "").strip()
        parsed = urllib.parse.urlsplit(identity_text)
        if parsed.scheme != "spiffe" or not parsed.netloc or parsed.query or parsed.fragment:
            raise GatewayError("policy_invalid", "workload identity must be a SPIFFE URI")
        if not isinstance(raw, dict) or set(raw) - {
            "providers",
            "max_lease_budget_nano_cny",
            "max_lease_ttl_seconds",
        }:
            raise GatewayError("policy_invalid", f"workload {identity_text} is invalid")
        allowed = raw.get("providers")
        if (
            not isinstance(allowed, list)
            or not allowed
            or any(item not in providers for item in allowed)
            or len(set(allowed)) != len(allowed)
        ):
            raise GatewayError("policy_invalid", f"workload {identity_text} providers are invalid")
        workloads[identity_text] = WorkloadPolicy(
            identity=identity_text,
            providers=tuple(sorted(allowed)),
            max_lease_budget_nano_cny=_positive_int(
                raw.get("max_lease_budget_nano_cny"),
                "max lease budget",
            ),
            max_lease_ttl_seconds=_positive_int(
                raw.get("max_lease_ttl_seconds", default_ttl),
                "max lease ttl",
                maximum=3600,
            ),
        )
    return workloads


class LeaseSigner:
    """Compact HMAC lease tokens with strict algorithm and claim validation."""

    def __init__(self, key: bytes, *, issuer: str, audience: str) -> None:
        if len(key) < 32:
            raise GatewayError("signing_key_invalid", "lease signing key must be at least 32 bytes")
        self._key = bytes(key)
        self.issuer = issuer
        self.audience = audience

    def issue(self, claims: Mapping[str, Any]) -> str:
        header = {"alg": "HS256", "typ": "CMLT", "v": 1}
        body = dict(claims)
        encoded = f"{_b64url(_canonical_bytes(header))}.{_b64url(_canonical_bytes(body))}"
        signature = hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).digest()
        return f"{encoded}.{_b64url(signature)}"

    def verify(self, token: str, *, now: int | None = None) -> dict[str, Any]:
        parts = token.split(".")
        if len(parts) != 3:
            raise GatewayError("lease_invalid", "lease token structure is invalid", status=401)
        signed = f"{parts[0]}.{parts[1]}".encode("ascii", errors="strict")
        expected = hmac.new(self._key, signed, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(parts[2])):
            raise GatewayError("lease_invalid", "lease token signature is invalid", status=401)
        try:
            header = json.loads(_b64url_decode(parts[0]))
            claims = json.loads(_b64url_decode(parts[1]))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GatewayError("lease_invalid", "lease token JSON is invalid", status=401) from exc
        current = int(time.time() if now is None else now)
        if (
            isinstance(claims, dict)
            and isinstance(claims.get("exp"), int)
            and claims["exp"] <= current
        ):
            raise GatewayError("lease_expired", "lease token is expired", status=401)
        if (
            header != {"alg": "HS256", "typ": "CMLT", "v": 1}
            or not isinstance(claims, dict)
            or claims.get("schema_version") != LEASE_SCHEMA
            or claims.get("iss") != self.issuer
            or claims.get("aud") != self.audience
            or not isinstance(claims.get("iat"), int)
            or not isinstance(claims.get("exp"), int)
            or claims["iat"] > current + 30
            or claims["exp"] <= current
            or claims["exp"] - claims["iat"] > 3600
            or not _SAFE_NAME.fullmatch(str(claims.get("jti") or ""))
        ):
            raise GatewayError("lease_invalid", "lease token claims are invalid", status=401)
        return claims


class GatewayLedger:
    """SQLite-backed lease and request ledger.

    All monetary values are integer nano-CNY.  Reservation happens in a
    BEGIN IMMEDIATE transaction so concurrent proxy processes cannot admit
    requests beyond a lease budget.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with contextlib.closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS leases (
                    lease_id TEXT PRIMARY KEY,
                    workload_identity TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    claims_json TEXT NOT NULL,
                    claims_sha256 TEXT NOT NULL,
                    issued_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    budget_nano_cny INTEGER NOT NULL CHECK (budget_nano_cny > 0),
                    reserved_nano_cny INTEGER NOT NULL DEFAULT 0 CHECK (reserved_nano_cny >= 0),
                    spent_nano_cny INTEGER NOT NULL DEFAULT 0 CHECK (spent_nano_cny >= 0),
                    status TEXT NOT NULL CHECK (status IN ('active', 'expired', 'revoked', 'overrun')),
                    UNIQUE(workload_identity, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS proxy_requests (
                    request_id TEXT PRIMARY KEY,
                    lease_id TEXT NOT NULL REFERENCES leases(lease_id),
                    request_sha256 TEXT NOT NULL,
                    reserved_nano_cny INTEGER NOT NULL CHECK (reserved_nano_cny > 0),
                    actual_nano_cny INTEGER,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    state TEXT NOT NULL CHECK (state IN ('reserved', 'dispatched', 'settled', 'released', 'overrun')),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS proxy_requests_lease
                    ON proxy_requests(lease_id, state);
                """
            )

    def create_or_replay_lease(
        self,
        *,
        workload_identity: str,
        idempotency_key: str,
        request_sha256: str,
        claims: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        claims_json = _canonical_bytes(claims).decode("utf-8")
        claims_sha256 = _sha256(claims)
        with contextlib.closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT request_sha256, claims_json
                FROM leases
                WHERE workload_identity=? AND idempotency_key=?
                """,
                (workload_identity, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha256:
                    connection.rollback()
                    raise GatewayError(
                        "idempotency_conflict",
                        "lease idempotency key was reused with different input",
                        status=409,
                    )
                connection.commit()
                return json.loads(existing["claims_json"]), True
            connection.execute(
                """
                INSERT INTO leases (
                    lease_id, workload_identity, idempotency_key,
                    request_sha256, claims_json, claims_sha256,
                    issued_at, expires_at, budget_nano_cny, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    claims["jti"],
                    workload_identity,
                    idempotency_key,
                    request_sha256,
                    claims_json,
                    claims_sha256,
                    claims["iat"],
                    claims["exp"],
                    claims["budget_nano_cny"],
                ),
            )
            connection.commit()
        return dict(claims), False

    def reserve(
        self,
        *,
        claims: Mapping[str, Any],
        request_id: str,
        request_sha256: str,
        amount_nano_cny: int,
        now: int,
    ) -> None:
        lease_id = str(claims["jti"])
        with contextlib.closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT lease_id, request_sha256 FROM proxy_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                if (
                    existing["lease_id"] == lease_id
                    and existing["request_sha256"] == request_sha256
                ):
                    raise GatewayError(
                        "request_replay",
                        "request id was already admitted; provider call will not be repeated",
                        status=409,
                    )
                raise GatewayError(
                    "idempotency_conflict",
                    "request id was reused with different input",
                    status=409,
                )
            lease = connection.execute(
                "SELECT * FROM leases WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
            if (
                lease is None
                or lease["claims_sha256"] != _sha256(claims)
                or lease["status"] != "active"
                or lease["expires_at"] <= now
            ):
                connection.rollback()
                raise GatewayError("lease_inactive", "lease is missing, expired, or revoked", status=401)
            available = (
                int(lease["budget_nano_cny"])
                - int(lease["reserved_nano_cny"])
                - int(lease["spent_nano_cny"])
            )
            if amount_nano_cny > available:
                connection.rollback()
                raise GatewayError(
                    "budget_exhausted",
                    "request worst-case cost exceeds the remaining lease budget",
                    status=402,
                )
            connection.execute(
                """
                INSERT INTO proxy_requests (
                    request_id, lease_id, request_sha256,
                    reserved_nano_cny, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (request_id, lease_id, request_sha256, amount_nano_cny, now, now),
            )
            connection.execute(
                "UPDATE leases SET reserved_nano_cny=reserved_nano_cny+? WHERE lease_id=?",
                (amount_nano_cny, lease_id),
            )
            connection.commit()

    def mark_dispatched(self, request_id: str, *, now: int) -> None:
        with contextlib.closing(self._connect()) as connection:
            changed = connection.execute(
                """
                UPDATE proxy_requests
                SET state='dispatched', updated_at=?
                WHERE request_id=? AND state='reserved'
                """,
                (now, request_id),
            ).rowcount
            if changed != 1:
                raise GatewayError("request_state_invalid", "request reservation is not dispatchable", status=409)

    def release(self, request_id: str, *, now: int) -> None:
        with contextlib.closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM proxy_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None or row["state"] != "reserved":
                connection.rollback()
                raise GatewayError("request_state_invalid", "only an undispatched reservation can be released")
            connection.execute(
                """
                UPDATE leases SET reserved_nano_cny=reserved_nano_cny-?
                WHERE lease_id=?
                """,
                (row["reserved_nano_cny"], row["lease_id"]),
            )
            connection.execute(
                "UPDATE proxy_requests SET state='released', updated_at=? WHERE request_id=?",
                (now, request_id),
            )
            connection.commit()

    def settle(
        self,
        request_id: str,
        *,
        actual_nano_cny: int,
        input_tokens: int | None,
        output_tokens: int | None,
        now: int,
    ) -> dict[str, Any]:
        if actual_nano_cny < 0:
            raise GatewayError("settlement_invalid", "actual cost cannot be negative")
        with contextlib.closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM proxy_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None or row["state"] != "dispatched":
                connection.rollback()
                raise GatewayError("request_state_invalid", "request is not awaiting settlement", status=409)
            reserved = int(row["reserved_nano_cny"])
            state = "settled" if actual_nano_cny <= reserved else "overrun"
            connection.execute(
                """
                UPDATE leases
                SET reserved_nano_cny=reserved_nano_cny-?,
                    spent_nano_cny=spent_nano_cny+?,
                    status=CASE WHEN ?='overrun' THEN 'overrun' ELSE status END
                WHERE lease_id=?
                """,
                (reserved, actual_nano_cny, state, row["lease_id"]),
            )
            connection.execute(
                """
                UPDATE proxy_requests
                SET actual_nano_cny=?, input_tokens=?, output_tokens=?,
                    state=?, updated_at=?
                WHERE request_id=?
                """,
                (
                    actual_nano_cny,
                    input_tokens,
                    output_tokens,
                    state,
                    now,
                    request_id,
                ),
            )
            lease = connection.execute(
                """
                SELECT budget_nano_cny, reserved_nano_cny, spent_nano_cny, status
                FROM leases WHERE lease_id=?
                """,
                (row["lease_id"],),
            ).fetchone()
            connection.commit()
        return {
            "state": state,
            "reserved_nano_cny": reserved,
            "actual_nano_cny": actual_nano_cny,
            "lease": dict(lease),
        }


class AuditLog:
    """Secret-free append-only JSONL audit stream."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        if self.path is None:
            return
        row = {
            "schema_version": "costmarshal-gateway-audit-v1",
            "timestamp": int(time.time()),
            "event": event,
            **fields,
        }
        payload = _canonical_bytes(row) + b"\n"
        with self._lock:
            with self.path.open("ab") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())


class BrokerCore:
    def __init__(
        self,
        policy: GatewayPolicy,
        signer: LeaseSigner,
        ledger: GatewayLedger,
        audit: AuditLog,
    ) -> None:
        self.policy = policy
        self.signer = signer
        self.ledger = ledger
        self.audit = audit

    def issue(
        self,
        workload_identity: str,
        request: Mapping[str, Any],
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        current = int(time.time() if now is None else now)
        workload = self.policy.workloads.get(workload_identity)
        if workload is None:
            raise GatewayError("workload_forbidden", "workload identity is not allowlisted", status=403)
        if set(request) - {
            "provider",
            "model",
            "budget_nano_cny",
            "max_input_tokens",
            "max_output_tokens",
            "ttl_seconds",
            "idempotency_key",
        }:
            raise GatewayError("lease_request_invalid", "lease request has unknown fields")
        provider_id = _require_name(request.get("provider"), "provider")
        model_id = _require_name(request.get("model"), "model")
        if provider_id not in workload.providers:
            raise GatewayError("provider_forbidden", "provider is not allowed for this workload", status=403)
        provider = self.policy.providers[provider_id]
        if model_id not in provider.models:
            raise GatewayError("model_forbidden", "model is not allowed for this provider", status=403)
        budget = _positive_int(request.get("budget_nano_cny"), "lease budget")
        if budget > workload.max_lease_budget_nano_cny:
            raise GatewayError("budget_forbidden", "lease budget exceeds the workload policy", status=403)
        price = provider.models[model_id]
        max_input_tokens = _positive_int(
            request.get("max_input_tokens"),
            "max input tokens",
            maximum=100_000_000,
        )
        max_output_tokens = _positive_int(
            request.get("max_output_tokens"),
            "max output tokens",
            maximum=price.max_output_tokens,
        )
        minimum_budget = _reservation_cost(
            price,
            input_tokens=max_input_tokens + price.request_overhead_tokens,
            output_tokens=max_output_tokens,
        )
        if minimum_budget > budget:
            raise GatewayError(
                "budget_too_small",
                "lease budget cannot cover the declared token envelope",
                status=402,
            )
        ttl = _positive_int(
            request.get("ttl_seconds", self.policy.lease_ttl_seconds),
            "lease ttl",
            maximum=min(workload.max_lease_ttl_seconds, self.policy.lease_ttl_seconds),
        )
        idempotency_key = str(request.get("idempotency_key") or "").strip()
        if not _REQUEST_ID.fullmatch(idempotency_key):
            raise GatewayError("lease_request_invalid", "idempotency_key is invalid")
        request_material = {
            "workload_identity": workload_identity,
            "provider": provider_id,
            "model": model_id,
            "budget_nano_cny": budget,
            "max_input_tokens": max_input_tokens,
            "max_output_tokens": max_output_tokens,
            "ttl_seconds": ttl,
            "idempotency_key": idempotency_key,
        }
        request_sha256 = _sha256(request_material)
        lease_id = "LSE-" + hashlib.sha256(
            f"{workload_identity}\0{idempotency_key}\0{request_sha256}".encode("utf-8")
        ).hexdigest()[:32]
        proposed = {
            "schema_version": LEASE_SCHEMA,
            "iss": self.policy.issuer,
            "aud": self.policy.audience,
            "jti": lease_id,
            "sub": workload_identity,
            "provider": provider_id,
            "model": model_id,
            "budget_nano_cny": budget,
            "input_nano_cny_per_million": price.input_nano_cny_per_million,
            "output_nano_cny_per_million": price.output_nano_cny_per_million,
            "fixed_nano_cny_per_request": price.fixed_nano_cny_per_request,
            "billing_safety_ppm": price.billing_safety_ppm,
            "max_input_tokens": max_input_tokens,
            "max_output_tokens": max_output_tokens,
            "iat": current,
            "exp": current + ttl,
            "policy_sha256": self.policy.sha256,
        }
        claims, replayed = self.ledger.create_or_replay_lease(
            workload_identity=workload_identity,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            claims=proposed,
        )
        token = self.signer.issue(claims)
        self.audit.write(
            "lease_replayed" if replayed else "lease_issued",
            lease_id=claims["jti"],
            workload_sha256=_sha256(workload_identity.encode("utf-8")),
            provider=provider_id,
            model=model_id,
            budget_nano_cny=budget,
            expires_at=claims["exp"],
        )
        return {
            "schema_version": LEASE_SCHEMA,
            "lease_token": token,
            "token_type": "Bearer",
            "expires_at": claims["exp"],
            "lease_id": claims["jti"],
            "provider": provider_id,
            "model": model_id,
            "budget_nano_cny": budget,
            "replayed": replayed,
        }


@dataclass(frozen=True)
class PreparedProxyRequest:
    request_id: str
    claims: Mapping[str, Any]
    provider: ProviderPolicy
    model_price: ModelPrice
    path: str
    payload: bytes
    payload_sha256: str
    reserved_nano_cny: int
    max_output_tokens: int
    streaming: bool


class ProviderProxyCore:
    def __init__(
        self,
        policy: GatewayPolicy,
        signer: LeaseSigner,
        ledger: GatewayLedger,
        audit: AuditLog,
    ) -> None:
        self.policy = policy
        self.signer = signer
        self.ledger = ledger
        self.audit = audit

    def prepare(
        self,
        *,
        token: str,
        request_id: str,
        path: str,
        payload: bytes,
        now: int | None = None,
    ) -> PreparedProxyRequest:
        current = int(time.time() if now is None else now)
        if not _REQUEST_ID.fullmatch(request_id):
            raise GatewayError("request_id_invalid", "X-CostMarshal-Request-Id is invalid")
        if path not in _ALLOWED_PATHS:
            raise GatewayError("path_forbidden", "provider path is not allowlisted", status=404)
        if len(payload) > _MAX_JSON_BYTES:
            raise GatewayError("request_too_large", "provider request exceeds 4 MiB", status=413)
        try:
            body = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError("request_invalid", "provider request must be UTF-8 JSON") from exc
        if not isinstance(body, dict):
            raise GatewayError("request_invalid", "provider request must be a JSON object")
        claims = self.signer.verify(token, now=current)
        if claims.get("policy_sha256") != self.policy.sha256:
            raise GatewayError("lease_stale", "lease was issued for a different gateway policy", status=401)
        provider_id = str(claims.get("provider") or "")
        model_id = str(claims.get("model") or "")
        provider = self.policy.providers.get(provider_id)
        if provider is None or model_id not in provider.models:
            raise GatewayError("lease_scope_invalid", "lease provider or model is unavailable", status=401)
        if body.get("model") != model_id:
            raise GatewayError("model_scope_violation", "request model does not match the lease", status=403)
        output_field = "max_output_tokens" if path == "/v1/responses" else "max_tokens"
        if output_field not in body:
            body[output_field] = int(claims.get("max_output_tokens") or 0)
        max_output = _positive_int(body.get(output_field), output_field)
        configured_price = provider.models[model_id]
        if (
            claims.get("input_nano_cny_per_million")
            != configured_price.input_nano_cny_per_million
            or claims.get("output_nano_cny_per_million")
            != configured_price.output_nano_cny_per_million
            or claims.get("fixed_nano_cny_per_request")
            != configured_price.fixed_nano_cny_per_request
            or claims.get("billing_safety_ppm")
            != configured_price.billing_safety_ppm
        ):
            raise GatewayError(
                "lease_price_binding_invalid",
                "lease pricing does not match the reviewed gateway policy",
                status=401,
            )
        if (
            max_output > configured_price.max_output_tokens
            or max_output > int(claims.get("max_output_tokens") or 0)
        ):
            raise GatewayError("output_cap_exceeded", "requested output cap exceeds the reviewed policy")
        canonical_payload = _canonical_bytes(body)
        input_upper_bound = len(canonical_payload) + configured_price.request_overhead_tokens
        if input_upper_bound > int(claims.get("max_input_tokens") or 0):
            raise GatewayError(
                "input_cap_exceeded",
                "request input upper bound exceeds the lease token envelope",
                status=413,
            )
        reservation = _reservation_cost(
            configured_price,
            input_tokens=input_upper_bound,
            output_tokens=max_output,
        )
        request_sha256 = _sha256(
            {
                "lease_id": claims["jti"],
                "path": path,
                "payload_sha256": _sha256(canonical_payload),
            }
        )
        self.ledger.reserve(
            claims=claims,
            request_id=request_id,
            request_sha256=request_sha256,
            amount_nano_cny=reservation,
            now=current,
        )
        self.audit.write(
            "request_reserved",
            request_id=request_id,
            lease_id=claims["jti"],
            provider=provider_id,
            model=model_id,
            reserved_nano_cny=reservation,
            input_token_upper_bound=input_upper_bound,
            max_output_tokens=max_output,
            streaming=body.get("stream") is True,
        )
        return PreparedProxyRequest(
            request_id=request_id,
            claims=claims,
            provider=provider,
            model_price=configured_price,
            path=path,
            payload=canonical_payload,
            payload_sha256=_sha256(canonical_payload),
            reserved_nano_cny=reservation,
            max_output_tokens=max_output,
            streaming=body.get("stream") is True,
        )

    def credential_header(self, prepared: PreparedProxyRequest) -> tuple[str, str]:
        if prepared.provider.credential_file is not None:
            path = prepared.provider.credential_file
            try:
                if path.is_symlink() or not path.is_file():
                    raise OSError("credential file is not a regular file")
                payload = path.read_bytes()
            except OSError as exc:
                raise GatewayError(
                    "provider_credential_unavailable",
                    "proxy credential file is unavailable",
                    status=503,
                ) from exc
            if len(payload) > 256 * 1024:
                raise GatewayError(
                    "provider_credential_invalid",
                    "provider credential exceeds 256 KiB",
                    status=503,
                )
            try:
                secret = payload.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise GatewayError(
                    "provider_credential_invalid",
                    "provider credential is not UTF-8",
                    status=503,
                ) from exc
        else:
            secret = os.environ.get(str(prepared.provider.credential_env or ""))
        if not secret:
            raise GatewayError(
                "provider_credential_unavailable",
                "proxy provider credential is unavailable",
                status=503,
            )
        if "\r" in secret or "\n" in secret:
            raise GatewayError("provider_credential_invalid", "provider credential contains a newline", status=503)
        value = (
            f"{prepared.provider.auth_scheme} {secret}".strip()
            if prepared.provider.auth_scheme
            else secret
        )
        return prepared.provider.auth_header, value

    def upstream_url(self, prepared: PreparedProxyRequest) -> str:
        return prepared.provider.base_url + prepared.path

    def actual_cost(
        self,
        prepared: PreparedProxyRequest,
        response_payload: bytes,
    ) -> tuple[int, int | None, int | None]:
        usage = _extract_usage(response_payload)
        if usage is None:
            return prepared.reserved_nano_cny, None, None
        input_tokens, output_tokens = usage
        amount = (
            _token_cost(
                input_tokens,
                prepared.model_price.input_nano_cny_per_million,
            )
            + _token_cost(
                output_tokens,
                prepared.model_price.output_nano_cny_per_million,
            )
            + prepared.model_price.fixed_nano_cny_per_request
        )
        return amount, input_tokens, output_tokens


def _extract_usage(payload: bytes) -> tuple[int, int] | None:
    candidates: list[Mapping[str, Any]] = []
    with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            candidates.append(parsed)
    if payload.startswith(b"data:") or b"\ndata:" in payload:
        for line in payload.splitlines():
            if not line.startswith(b"data:"):
                continue
            chunk = line[5:].strip()
            if chunk == b"[DONE]":
                continue
            with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
                parsed = json.loads(chunk)
                if isinstance(parsed, dict):
                    candidates.append(parsed)
    for candidate in reversed(candidates):
        usage = candidate.get("usage")
        if not isinstance(usage, dict):
            response = candidate.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            continue
        input_value = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_value = usage.get("output_tokens", usage.get("completion_tokens"))
        if (
            isinstance(input_value, int)
            and not isinstance(input_value, bool)
            and input_value >= 0
            and isinstance(output_value, int)
            and not isinstance(output_value, bool)
            and output_value >= 0
        ):
            return input_value, output_value
    return None


def read_signing_key(path: Path) -> bytes:
    try:
        payload = path.read_bytes().strip()
    except OSError as exc:
        raise GatewayError("signing_key_unavailable", "lease signing key cannot be read") from exc
    if os.name != "nt":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise GatewayError("signing_key_permissions", "lease signing key must not be group/world accessible")
    decoded = payload
    if re.fullmatch(rb"[0-9a-fA-F]{64,}", payload) and len(payload) % 2 == 0:
        decoded = bytes.fromhex(payload.decode("ascii"))
    elif payload.startswith(b"base64:"):
        with contextlib.suppress(ValueError):
            decoded = base64.b64decode(payload[7:], validate=True)
    if len(decoded) < 32:
        raise GatewayError("signing_key_invalid", "lease signing key must contain at least 32 bytes")
    return decoded


def workload_identity_from_peer(handler: BaseHTTPRequestHandler) -> str:
    connection = getattr(handler, "connection", None)
    cert = connection.getpeercert() if connection is not None else None
    if not isinstance(cert, dict):
        raise GatewayError("client_certificate_required", "a verified mTLS client certificate is required", status=401)
    identities = sorted(
        value
        for kind, value in cert.get("subjectAltName", ())
        if kind == "URI" and isinstance(value, str) and value.startswith("spiffe://")
    )
    if len(identities) != 1:
        raise GatewayError(
            "workload_identity_invalid",
            "client certificate must contain exactly one SPIFFE URI SAN",
            status=401,
        )
    return identities[0]


def server_ssl_context(
    *,
    cert_file: Path,
    key_file: Path,
    client_ca_file: Path | None,
    require_client_certificate: bool,
) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert_file), str(key_file))
    context.options |= ssl.OP_NO_COMPRESSION
    if require_client_certificate:
        if client_ca_file is None:
            raise GatewayError("tls_config_invalid", "broker requires a client CA")
        context.load_verify_locations(cafile=str(client_ca_file))
        context.verify_mode = ssl.CERT_REQUIRED
    else:
        context.verify_mode = ssl.CERT_NONE
    return context


def request_provider_lease(
    *,
    endpoint: str,
    client_cert_file: Path,
    client_key_file: Path,
    ca_file: Path,
    provider: str,
    model: str,
    budget_nano_cny: int,
    max_input_tokens: int,
    max_output_tokens: int,
    idempotency_key: str,
    ttl_seconds: int = 300,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Acquire one lease over authenticated mTLS without reading provider keys."""

    parsed = urllib.parse.urlsplit(str(endpoint or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1/leases"
    ):
        raise GatewayError(
            "broker_endpoint_invalid",
            "broker endpoint must be a credential-free HTTPS /v1/leases URL",
        )
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=str(ca_file),
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(client_cert_file), str(client_key_file))
    request_body = {
        "provider": _require_name(provider, "provider"),
        "model": _require_name(model, "model"),
        "budget_nano_cny": _positive_int(budget_nano_cny, "lease budget"),
        "max_input_tokens": _positive_int(max_input_tokens, "max input tokens"),
        "max_output_tokens": _positive_int(max_output_tokens, "max output tokens"),
        "ttl_seconds": _positive_int(ttl_seconds, "lease ttl", maximum=3600),
        "idempotency_key": idempotency_key,
    }
    payload = _canonical_bytes(request_body)
    request = urllib.request.Request(
        parsed.geturl(),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Content-Length": str(len(payload)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request,
            context=context,
            timeout=timeout_seconds,
        ) as response:
            body = response.read(1024 * 1024 + 1)
            status = int(getattr(response, "status", 0))
    except urllib.error.HTTPError as exc:
        detail = exc.read(64 * 1024)
        with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
            error_row = json.loads(detail)
            message = str((error_row.get("error") or {}).get("message") or "")
            code = str((error_row.get("error") or {}).get("code") or "broker_rejected")
            raise GatewayError(code, message or "credential broker rejected the lease", status=exc.code)
        raise GatewayError("broker_rejected", "credential broker rejected the lease", status=exc.code) from exc
    except (OSError, urllib.error.URLError, ssl.SSLError) as exc:
        raise GatewayError("broker_unavailable", "credential broker is unavailable", status=503) from exc
    if status != HTTPStatus.CREATED or len(body) > 1024 * 1024:
        raise GatewayError("broker_response_invalid", "credential broker response is invalid", status=502)
    try:
        result = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayError("broker_response_invalid", "credential broker returned invalid JSON", status=502) from exc
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != LEASE_SCHEMA
        or result.get("token_type") != "Bearer"
        or result.get("provider") != provider
        or result.get("model") != model
        or result.get("budget_nano_cny") != budget_nano_cny
        or not isinstance(result.get("lease_token"), str)
        or not result["lease_token"]
        or len(result["lease_token"]) > 16_384
        or not _SAFE_NAME.fullmatch(str(result.get("lease_id") or ""))
        or not isinstance(result.get("expires_at"), int)
        or result["expires_at"] <= int(time.time())
    ):
        raise GatewayError("broker_response_invalid", "credential broker response binding is invalid", status=502)
    return result


def probe_gateway_health(
    *,
    endpoint: str,
    expected_policy_sha256: str,
    ca_file: Path,
    client_cert_file: Path | None = None,
    client_key_file: Path | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(str(endpoint or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise GatewayError("health_endpoint_invalid", "gateway health endpoint is invalid")
    health_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/healthz", "", "")
    )
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=str(ca_file),
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if (client_cert_file is None) != (client_key_file is None):
        raise GatewayError(
            "health_client_tls_invalid",
            "health probe client certificate and key must be paired",
        )
    if client_cert_file is not None and client_key_file is not None:
        context.load_cert_chain(str(client_cert_file), str(client_key_file))
    request = urllib.request.Request(
        health_url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request,
            context=context,
            timeout=timeout_seconds,
        ) as response:
            payload = response.read(64 * 1024 + 1)
            status = int(getattr(response, "status", 0))
    except (OSError, urllib.error.URLError, ssl.SSLError) as exc:
        raise GatewayError("gateway_health_unavailable", "gateway health probe failed", status=503) from exc
    try:
        row = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayError("gateway_health_invalid", "gateway health response is invalid", status=502) from exc
    if (
        status != 200
        or len(payload) > 64 * 1024
        or not isinstance(row, dict)
        or row.get("schema_version") != HEALTH_SCHEMA
        or row.get("status") != "ok"
        or row.get("policy_sha256") != expected_policy_sha256
    ):
        raise GatewayError(
            "gateway_health_binding_failed",
            "gateway health response does not match the reviewed policy",
            status=503,
        )
    return {
        "status": "pass",
        "endpoint": health_url,
        "policy_sha256": expected_policy_sha256,
    }


class GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        core: BrokerCore | ProviderProxyCore,
        policy: GatewayPolicy,
    ) -> None:
        super().__init__(address, handler)
        self.core = core
        self.policy = policy


class _BaseHandler(BaseHTTPRequestHandler):
    server: GatewayHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "CostMarshalGateway/1"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self, status: int, value: Mapping[str, Any]) -> None:
        payload = _canonical_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, exc: GatewayError) -> None:
        self._json(
            exc.status,
            {
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                }
            },
        )

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise GatewayError("content_length_required", "Content-Length is required", status=411)
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise GatewayError("content_length_invalid", "Content-Length is invalid") from exc
        if length < 0 or length > _MAX_JSON_BYTES:
            raise GatewayError("request_too_large", "request exceeds 4 MiB", status=413)
        payload = self.rfile.read(length)
        if len(payload) != length:
            raise GatewayError("request_truncated", "request body is truncated")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        if urllib.parse.urlsplit(self.path).path != "/healthz":
            self._json(404, {"error": {"code": "not_found", "message": "not found"}})
            return
        self._json(
            200,
            {
                "schema_version": HEALTH_SCHEMA,
                "status": "ok",
                "policy_sha256": self.server.policy.sha256,
            },
        )


class BrokerHandler(_BaseHandler):
    def do_POST(self) -> None:  # noqa: N802
        try:
            if urllib.parse.urlsplit(self.path).path != "/v1/leases":
                raise GatewayError("not_found", "not found", status=404)
            identity = workload_identity_from_peer(self)
            payload = self._read_body()
            request = json.loads(payload)
            if not isinstance(request, dict):
                raise GatewayError("lease_request_invalid", "lease request must be an object")
            assert isinstance(self.server.core, BrokerCore)
            self._json(201, self.server.core.issue(identity, request))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(GatewayError("lease_request_invalid", "lease request must be UTF-8 JSON"))
        except GatewayError as exc:
            self._error(exc)


class ProxyHandler(_BaseHandler):
    def do_POST(self) -> None:  # noqa: N802
        prepared: PreparedProxyRequest | None = None
        dispatched = False
        response_started = False
        core = self.server.core
        assert isinstance(core, ProviderProxyCore)
        try:
            authorization = str(self.headers.get("Authorization") or "")
            if not authorization.startswith("Bearer ") or len(authorization) > 16384:
                raise GatewayError("lease_required", "a Bearer lease token is required", status=401)
            token = authorization[7:].strip()
            request_id = str(self.headers.get("X-CostMarshal-Request-Id") or "").strip()
            path = urllib.parse.urlsplit(self.path)
            if path.query or path.fragment:
                raise GatewayError("path_forbidden", "query and fragment are forbidden", status=404)
            prepared = core.prepare(
                token=token,
                request_id=request_id,
                path=path.path,
                payload=self._read_body(),
            )
            try:
                credential_header, credential_value = core.credential_header(prepared)
            except GatewayError:
                core.ledger.release(prepared.request_id, now=int(time.time()))
                raise
            upstream_headers = {
                "Content-Type": "application/json",
                "Accept": str(self.headers.get("Accept") or "application/json"),
                credential_header: credential_value,
                "X-Request-Id": prepared.request_id,
            }
            request = urllib.request.Request(
                core.upstream_url(prepared),
                data=prepared.payload,
                headers=upstream_headers,
                method="POST",
            )
            core.ledger.mark_dispatched(prepared.request_id, now=int(time.time()))
            dispatched = True
            try:
                response = urllib.request.urlopen(request, timeout=300)  # noqa: S310
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = int(getattr(response, "status", 502))
                content_type = str(response.headers.get("Content-Type") or "application/json")
                if prepared.streaming:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header(
                        "X-CostMarshal-Request-Id",
                        prepared.request_id,
                    )
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Trailer", "X-CostMarshal-Settlement")
                    self.end_headers()
                    response_started = True
                    chunks: list[bytes] = []
                    response_size = 0
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        response_size += len(chunk)
                        if response_size > _MAX_UPSTREAM_RESPONSE_BYTES:
                            raise GatewayError(
                                "upstream_response_too_large",
                                "provider response exceeds 64 MiB",
                                status=502,
                            )
                        chunks.append(chunk)
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    response_payload = b"".join(chunks)
                else:
                    response_payload = response.read(
                        _MAX_UPSTREAM_RESPONSE_BYTES + 1
                    )
                    if len(response_payload) > _MAX_UPSTREAM_RESPONSE_BYTES:
                        raise GatewayError(
                            "upstream_response_too_large",
                            "provider response exceeds 64 MiB",
                            status=502,
                        )
            actual, input_tokens, output_tokens = core.actual_cost(prepared, response_payload)
            settlement = core.ledger.settle(
                prepared.request_id,
                actual_nano_cny=actual,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                now=int(time.time()),
            )
            core.audit.write(
                "request_settled",
                request_id=prepared.request_id,
                lease_id=prepared.claims["jti"],
                provider=prepared.claims["provider"],
                model=prepared.claims["model"],
                status=status,
                reserved_nano_cny=settlement["reserved_nano_cny"],
                actual_nano_cny=settlement["actual_nano_cny"],
                settlement_state=settlement["state"],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            if prepared.streaming:
                self.wfile.write(b"0\r\n")
                self.wfile.write(
                    (
                        "X-CostMarshal-Settlement: "
                        + settlement["state"]
                        + "\r\n\r\n"
                    ).encode("ascii")
                )
                self.wfile.flush()
            else:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(response_payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-CostMarshal-Request-Id", prepared.request_id)
                self.send_header("X-CostMarshal-Settlement", settlement["state"])
                self.end_headers()
                self.wfile.write(response_payload)
        except GatewayError as exc:
            if prepared is not None and dispatched:
                with contextlib.suppress(GatewayError):
                    settlement = core.ledger.settle(
                        prepared.request_id,
                        actual_nano_cny=prepared.reserved_nano_cny,
                        input_tokens=None,
                        output_tokens=None,
                        now=int(time.time()),
                    )
                    core.audit.write(
                        "request_settlement_conservative",
                        request_id=prepared.request_id,
                        lease_id=prepared.claims["jti"],
                        reserved_nano_cny=settlement["reserved_nano_cny"],
                        error_code=exc.code,
                    )
            if response_started:
                self.close_connection = True
            else:
                self._error(exc)
        except (OSError, urllib.error.URLError) as exc:
            if prepared is not None and dispatched:
                with contextlib.suppress(GatewayError):
                    core.ledger.settle(
                        prepared.request_id,
                        actual_nano_cny=prepared.reserved_nano_cny,
                        input_tokens=None,
                        output_tokens=None,
                        now=int(time.time()),
                    )
            if response_started:
                self.close_connection = True
            else:
                self._error(
                    GatewayError(
                        "upstream_unavailable",
                        "provider upstream is unavailable",
                        status=502,
                    )
                )


def build_gateway(
    *,
    service: str,
    policy: GatewayPolicy,
    signing_key: bytes,
    audit_log: Path | None,
    listen_host: str,
    listen_port: int,
) -> GatewayHTTPServer:
    signer = LeaseSigner(signing_key, issuer=policy.issuer, audience=policy.audience)
    ledger = GatewayLedger(policy.database_path)
    audit = AuditLog(audit_log)
    if service == "broker":
        core: BrokerCore | ProviderProxyCore = BrokerCore(policy, signer, ledger, audit)
        handler = BrokerHandler
    elif service == "proxy":
        core = ProviderProxyCore(policy, signer, ledger, audit)
        handler = ProxyHandler
    else:
        raise GatewayError("service_invalid", "service must be broker or proxy")
    return GatewayHTTPServer(
        (listen_host, listen_port),
        handler,
        core=core,
        policy=policy,
    )


def serve_gateway(
    *,
    service: str,
    policy_path: Path,
    signing_key_path: Path,
    cert_file: Path,
    key_file: Path,
    client_ca_file: Path | None,
    audit_log: Path | None,
    listen_host: str,
    listen_port: int,
) -> None:
    policy = GatewayPolicy.load(policy_path)
    server = build_gateway(
        service=service,
        policy=policy,
        signing_key=read_signing_key(signing_key_path),
        audit_log=audit_log,
        listen_host=listen_host,
        listen_port=listen_port,
    )
    context = server_ssl_context(
        cert_file=cert_file,
        key_file=key_file,
        client_ca_file=client_ca_file,
        require_client_certificate=service == "broker",
    )
    server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


__all__ = [
    "AuditLog",
    "BrokerCore",
    "GatewayError",
    "GatewayLedger",
    "GatewayPolicy",
    "HEALTH_SCHEMA",
    "LEASE_SCHEMA",
    "LeaseSigner",
    "POLICY_SCHEMA",
    "PreparedProxyRequest",
    "ProviderProxyCore",
    "build_gateway",
    "read_signing_key",
    "probe_gateway_health",
    "request_provider_lease",
    "serve_gateway",
    "server_ssl_context",
    "workload_identity_from_peer",
]
