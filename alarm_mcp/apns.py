"""APNs (Apple Push Notification service) sender — token-based auth (.p8).

Uses the modern HTTP/2 + JWT flow with a single .p8 Auth Key, which works for
both sandbox (Xcode debug builds) and production.

Env vars required:
  ALARM_MCP_APNS_KEY_ID     — 10-char Key ID from Apple Developer portal
  ALARM_MCP_APNS_TEAM_ID    — 10-char Team ID
  ALARM_MCP_APNS_BUNDLE_ID  — your app's bundle id (e.g. com.you.wakeupwhen)
  ALARM_MCP_APNS_KEY_P8     — full .p8 file contents (-----BEGIN PRIVATE KEY-----...)
  ALARM_MCP_APNS_ENV        — "sandbox" (default) or "production"

Device tokens are kept in the same state dir as alarms, in apns_tokens.json.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

import httpx


# ---------------------------------------------------------------------------
# JWT signing for APNs (ES256 over the .p8 EC private key)
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _load_ec_private_key(pem: str):
    # cryptography is a transitive dep of httpx[http2]; if missing, import will raise.
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    return load_pem_private_key(pem.encode(), password=None)


def _make_jwt(key_id: str, team_id: str, p8_pem: str) -> str:
    """Build a fresh APNs provider JWT (ES256). Valid ~50 minutes."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    payload = {"iss": team_id, "iat": int(time.time())}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )

    key = _load_ec_private_key(p8_pem)
    der_sig = key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    # APNs/JWT need fixed-length 32-byte r||s, not DER
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return signing_input + "." + _b64url(raw_sig)


# Cache JWT for ~45 min (APNs accepts max 1 hr per Apple docs).
_jwt_cache: dict[str, tuple[float, str]] = {}


def _cached_jwt(key_id: str, team_id: str, p8_pem: str) -> str:
    cache_key = key_id + ":" + hashlib.sha256(p8_pem.encode()).hexdigest()[:8]
    cached = _jwt_cache.get(cache_key)
    now = time.time()
    if cached and cached[0] > now:
        return cached[1]
    token = _make_jwt(key_id, team_id, p8_pem)
    _jwt_cache[cache_key] = (now + 45 * 60, token)
    return token


# ---------------------------------------------------------------------------
# Device token store
# ---------------------------------------------------------------------------

class TokenStore:
    """Persists APNs device tokens to disk (so they survive restarts)."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except Exception:
                self._data = {}

    def add(self, token: str, meta: Optional[dict] = None) -> None:
        token = token.strip()
        if not token:
            return
        self._data[token] = {
            "last_seen": time.time(),
            "meta": meta or {},
        }
        self._save()

    def remove(self, token: str) -> None:
        self._data.pop(token, None)
        self._save()

    def all(self) -> list[str]:
        return list(self._data.keys())

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.path)


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    return all(
        os.environ.get(k)
        for k in (
            "ALARM_MCP_APNS_KEY_ID",
            "ALARM_MCP_APNS_TEAM_ID",
            "ALARM_MCP_APNS_BUNDLE_ID",
            "ALARM_MCP_APNS_KEY_P8",
        )
    )


def _apns_host() -> str:
    env = os.environ.get("ALARM_MCP_APNS_ENV", "sandbox").lower()
    if env in ("prod", "production"):
        return "https://api.push.apple.com"
    return "https://api.sandbox.push.apple.com"


async def send_alarm_push(
    device_tokens: list[str],
    *,
    alarm_id: str,
    title: str,
    body: str,
    sound: str = "alarm.caf",
) -> dict:
    """Send a high-priority alarm push to a list of device tokens.

    Returns a dict {token: status_code_or_error}.
    """
    if not is_configured():
        return {t: "apns_not_configured" for t in device_tokens}
    if not device_tokens:
        return {}

    key_id = os.environ["ALARM_MCP_APNS_KEY_ID"]
    team_id = os.environ["ALARM_MCP_APNS_TEAM_ID"]
    bundle_id = os.environ["ALARM_MCP_APNS_BUNDLE_ID"]
    p8_pem = os.environ["ALARM_MCP_APNS_KEY_P8"]

    jwt = _cached_jwt(key_id, team_id, p8_pem)
    host = _apns_host()

    payload = {
        "aps": {
            # mutable-content lets the app's Notification Service Extension
            # (if any) modify the push before display.
            "mutable-content": 1,
            # content-available wakes the app in the background to do work.
            "content-available": 1,
            "alert": {"title": title[:120], "body": body[:240]},
            "sound": {
                "critical": 0,  # set to 1 + entitlement to override silent mode
                "name": sound,
                "volume": 1.0,
            },
            "interruption-level": "time-sensitive",
        },
        # Custom keys our app reads to start the alarm-chain
        "alarm_id": alarm_id,
        "kind": "fire_alarm",
    }
    body_bytes = json.dumps(payload).encode()

    results: dict[str, str] = {}

    async with httpx.AsyncClient(http2=True, timeout=10) as client:

        async def send_one(token: str) -> None:
            url = f"{host}/3/device/{token}"
            headers = {
                "authorization": f"bearer {jwt}",
                "apns-topic": bundle_id,
                "apns-push-type": "alert",
                "apns-priority": "10",
                "apns-expiration": "0",
            }
            try:
                r = await client.post(url, headers=headers, content=body_bytes)
                if r.status_code == 200:
                    results[token] = "ok"
                else:
                    # APNs returns JSON like {"reason":"BadDeviceToken"}
                    reason = ""
                    try:
                        reason = r.json().get("reason", "")
                    except Exception:
                        reason = r.text[:120]
                    results[token] = f"{r.status_code}:{reason}"
            except Exception as e:
                results[token] = f"err:{type(e).__name__}:{e}"

        await asyncio.gather(*(send_one(t) for t in device_tokens))

    return results
