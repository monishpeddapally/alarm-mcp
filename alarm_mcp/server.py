"""FastMCP server exposing alarm tools + a background polling loop."""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from . import apns as apns_mod
from . import notifier as notifier_mod
from .evaluator import evaluate
from .models import Alarm, Store
from .notifier import fire
from .sources import fetch_snapshot


STATE_DIR = Path(os.environ.get("ALARM_MCP_STATE_DIR", Path.home() / ".alarm-mcp"))
STATE_FILE = STATE_DIR / "state.json"
APNS_TOKENS_FILE = STATE_DIR / "apns_tokens.json"

store = Store(STATE_FILE)
apns_tokens = apns_mod.TokenStore(APNS_TOKENS_FILE)
notifier_mod.set_apns_token_store(apns_tokens)
_lock = asyncio.Lock()
_poll_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Background polling loop
# ---------------------------------------------------------------------------

async def _check_one(alarm: Alarm) -> None:
    try:
        snapshot = await fetch_snapshot(alarm.condition, alarm.source_hint)
        verdict = await evaluate(alarm.condition, snapshot)
    except Exception as e:
        async with _lock:
            alarm.error = f"{type(e).__name__}: {e}"
            alarm.last_checked_at = time.time()
            alarm.check_count += 1
            store.put(alarm)
        return

    async with _lock:
        alarm.last_checked_at = time.time()
        alarm.check_count += 1
        alarm.last_check_summary = verdict.summary
        alarm.error = None
        if alarm.status == "pending":
            alarm.status = "armed"
        if verdict.fired and verdict.confidence >= 0.6:
            alarm.status = "triggered"
            alarm.triggered_at = time.time()
            alarm.last_evidence = verdict.evidence
        store.put(alarm)

    if alarm.status == "triggered":
        await fire(alarm, evidence=verdict.evidence)


async def _poll_loop() -> None:
    """Single loop that checks each active alarm on its own cadence."""
    next_due: dict[str, float] = {}
    while True:
        try:
            now = time.time()
            active = [a for a in store.active()]
            due = [a for a in active if next_due.get(a.id, 0) <= now]
            if due:
                await asyncio.gather(*[_check_one(a) for a in due], return_exceptions=True)
                for a in due:
                    next_due[a.id] = time.time() + max(5, a.poll_seconds)
            # housekeep next_due
            for aid in list(next_due.keys()):
                if aid not in {a.id for a in active}:
                    next_due.pop(aid, None)
        except Exception as e:
            print(f"[alarm-mcp] poll loop error: {e}", flush=True)
        await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# FastMCP server + lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastMCP):
    global _poll_task
    _poll_task = asyncio.create_task(_poll_loop())
    try:
        yield {}
    finally:
        if _poll_task:
            _poll_task.cancel()
            try:
                await _poll_task
            except (asyncio.CancelledError, Exception):
                pass


mcp = FastMCP(
    name="alarm-mcp",
    instructions=(
        "An event-driven alarm server. Use create_alarm to register a "
        "natural-language condition like 'Rishabh Pant comes to bat' or "
        "'Bitcoin drops below $50,000' or 'Trump is currently speaking'. "
        "The server polls live data and fires a real alarm (sound + "
        "notification) when the condition becomes true."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool
async def create_alarm(
    condition: str,
    label: str = "",
    source_hint: Optional[str] = None,
    poll_seconds: int = 30,
) -> dict:
    """Create an event-driven alarm.

    Args:
        condition: Natural-language event to watch for. Examples:
            "Rishabh Pant comes to bat"
            "India wins the current match"
            "Bitcoin drops below 50000 USD"
            "Donald Trump is currently giving a live speech"
        label: Short label shown when the alarm fires (defaults to the condition).
        source_hint: Optional source override. Values:
            'cricket'         — force the cricbuzz source
            'price:bitcoin'   — force a coingecko price snapshot
            'news'            — force a generic web snapshot
            None              — auto-route based on keywords
        poll_seconds: How often to check. Min 5s. Cricket: 15-30s recommended.
            News/price: 60-300s recommended.
    """
    poll_seconds = max(5, int(poll_seconds))
    alarm = Alarm(
        condition=condition.strip(),
        label=(label or condition).strip(),
        source_hint=source_hint,
        poll_seconds=poll_seconds,
    )
    async with _lock:
        store.put(alarm)
    return alarm.model_dump()


@mcp.tool
async def list_alarms(include_done: bool = True) -> list[dict]:
    """List every alarm and its current state."""
    async with _lock:
        items = store.all()
    if not include_done:
        items = [a for a in items if a.status in ("pending", "armed")]
    items.sort(key=lambda a: a.created_at, reverse=True)
    return [a.model_dump() for a in items]


@mcp.tool
async def get_alarm(alarm_id: str) -> Optional[dict]:
    """Return the full state of a single alarm (last check, evidence, etc.)."""
    async with _lock:
        a = store.get(alarm_id)
    return a.model_dump() if a else None


@mcp.tool
async def cancel_alarm(alarm_id: str) -> dict:
    """Cancel and delete an alarm."""
    async with _lock:
        existed = store.delete(alarm_id)
    return {"deleted": existed, "alarm_id": alarm_id}


@mcp.tool
async def test_trigger(alarm_id: str) -> dict:
    """Force-fire an alarm right now to verify your notification setup works."""
    async with _lock:
        a = store.get(alarm_id)
    if not a:
        return {"ok": False, "error": "not found"}
    a.status = "triggered"
    a.triggered_at = time.time()
    a.last_evidence = "manual test_trigger"
    async with _lock:
        store.put(a)
    used = await fire(a, evidence="manual test_trigger")
    return {"ok": True, "channels": used}


@mcp.tool
async def check_now(alarm_id: str) -> dict:
    """Run one immediate check on an alarm without waiting for the next poll."""
    async with _lock:
        a = store.get(alarm_id)
    if not a:
        return {"ok": False, "error": "not found"}
    await _check_one(a)
    async with _lock:
        a = store.get(alarm_id)
    return {"ok": True, "alarm": a.model_dump() if a else None}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _build_http_app():
    """Build a Starlette ASGI app with bearer-token auth.

    We use a raw ASGI middleware (not BaseHTTPMiddleware) because the MCP
    streamable-http transport relies on long-lived streaming responses,
    which BaseHTTPMiddleware buffers and breaks.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, JSONResponse
    from starlette.routing import Mount, Route

    expected_token = os.environ.get("ALARM_MCP_TOKEN", "").strip()

    class BearerAuthASGI:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return
            path = scope.get("path", "")
            if path in ("/health", "/") or path.startswith("/.well-known/"):
                await self.app(scope, receive, send)
                return
            if not expected_token:
                resp = JSONResponse(
                    {"error": "server is missing ALARM_MCP_TOKEN env var"},
                    status_code=503,
                )
                await resp(scope, receive, send)
                return
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            auth = headers.get("authorization", "")
            ok = (
                auth.lower().startswith("bearer ")
                and auth.split(None, 1)[1].strip() == expected_token
            )
            if not ok:
                resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                await resp(scope, receive, send)
                return
            await self.app(scope, receive, send)

    async def health(_request):
        return PlainTextResponse("ok")

    async def register_device(request: Request):
        """Wake Up When iOS app POSTs its APNs device token here."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        token = (body.get("token") or "").strip()
        if not token or len(token) < 32:
            return JSONResponse({"error": "missing or invalid 'token'"}, status_code=400)
        meta = {
            "platform": body.get("platform", "ios"),
            "app_version": body.get("app_version"),
            "env": body.get("env"),  # "sandbox" or "production"
        }
        apns_tokens.add(token, meta=meta)
        return JSONResponse({"ok": True, "registered_tokens": len(apns_tokens.all())})

    async def unregister_device(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        token = (body.get("token") or "").strip()
        if token:
            apns_tokens.remove(token)
        return JSONResponse({"ok": True, "registered_tokens": len(apns_tokens.all())})

    async def dismiss_alarm(request: Request):
        """App calls this when the user taps Dismiss, so the server clears state."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        alarm_id = (body.get("alarm_id") or "").strip()
        if not alarm_id:
            return JSONResponse({"error": "missing alarm_id"}, status_code=400)
        async def _do():
            async with _lock:
                a = store.get(alarm_id)
                if a:
                    a.status = "done"
                    store.put(a)
                    return True
                return False
        ok = await _do()
        return JSONResponse({"ok": ok})

    async def test_apns(_request):
        """Send a test push to every registered device. Handy for debugging."""
        tokens = apns_tokens.all()
        if not tokens:
            return JSONResponse({"error": "no devices registered"}, status_code=404)
        if not apns_mod.is_configured():
            return JSONResponse({"error": "APNs not configured on server"}, status_code=503)
        results = await apns_mod.send_alarm_push(
            tokens,
            alarm_id="test-" + str(int(time.time())),
            title="Wake Up When — Test",
            body="If you can read this, push routing works.",
        )
        return JSONResponse({"results": results, "count": len(tokens)})

    mcp_app = mcp.http_app(path="/mcp")
    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/", health),
            Route("/devices/register", register_device, methods=["POST"]),
            Route("/devices/unregister", unregister_device, methods=["POST"]),
            Route("/alarms/dismiss", dismiss_alarm, methods=["POST"]),
            Route("/debug/test-push", test_apns, methods=["POST", "GET"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=mcp_app.lifespan,
    )
    return BearerAuthASGI(app)


def main() -> None:
    """Default entry point.

    Transport is chosen by the ALARM_MCP_TRANSPORT env var:
      - "stdio" (default if unset)  → for local Claude Desktop / Cursor
      - "http"                       → for remote mobile clients
    """
    transport = os.environ.get("ALARM_MCP_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http", "sse"):
        import uvicorn
        host = os.environ.get("ALARM_MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", os.environ.get("ALARM_MCP_PORT", "8000")))
        uvicorn.run(_build_http_app(), host=host, port=port, log_level="info")
    else:
        mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
