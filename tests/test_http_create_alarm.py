"""Integration tests for the POST /alarms HTTP endpoint.

These tests exercise the Starlette ASGI app via httpx.ASGITransport so we
don't need to bind a real port. They use a temporary state dir so they
don't touch the user's ~/.alarm-mcp.

Run with:  python -m unittest tests.test_http_create_alarm
"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path


class TestHttpCreateAlarm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="alarm-mcp-test-")
        os.environ["ALARM_MCP_STATE_DIR"] = cls._tmpdir
        os.environ["ALARM_MCP_TOKEN"] = "test-token-abc"
        os.environ["ALARM_MCP_TRANSPORT"] = "http"
        # Force-reload server module so it picks up the env vars above
        import importlib
        import alarm_mcp.server as srv
        importlib.reload(srv)
        cls.srv = srv
        cls.app = srv._build_http_app()

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def _post(self, path: str, payload: dict, token: str = "test-token-abc"):
        import httpx
        transport = httpx.ASGITransport(app=self.app)

        async def go():
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                return await client.post(path, json=payload, headers=headers)

        return asyncio.new_event_loop().run_until_complete(go())

    def _get(self, path: str, token: str = "test-token-abc"):
        import httpx
        transport = httpx.ASGITransport(app=self.app)

        async def go():
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                return await client.get(path, headers=headers)

        return asyncio.new_event_loop().run_until_complete(go())

    # -- Tests -------------------------------------------------------------

    def test_create_cricket_alarm(self):
        r = self._post("/alarms", {
            "prompt": "wake me up when Rishabh Pant comes to bat",
            "device_token": "a" * 64,
        })
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["alarm"]["status"], "pending")
        self.assertEqual(body["alarm"]["source_hint"], "cricket")
        self.assertEqual(body["trigger"]["category"], "cricket")
        self.assertTrue(body["trigger"]["supported"])

    def test_create_price_alarm(self):
        r = self._post("/alarms/create", {
            "prompt": "wake me up when BTC falls below 50k",
        })
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["trigger"]["category"], "price")
        self.assertEqual(body["trigger"]["params"]["asset"], "bitcoin")
        self.assertEqual(body["trigger"]["params"]["threshold_usd"], 50_000)
        self.assertEqual(body["alarm"]["source_hint"], "price:bitcoin")

    def test_create_unknown_category_is_still_accepted(self):
        r = self._post("/alarms", {"prompt": "wake me up when my laundry is done"})
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["trigger"]["category"], "unknown")
        self.assertFalse(body["trigger"]["supported"])
        # the alarm itself is still created so the user/client gets a record
        self.assertIn("id", body["alarm"])

    def test_missing_prompt_returns_400(self):
        r = self._post("/alarms", {})
        self.assertEqual(r.status_code, 400)
        self.assertIn("prompt", r.json().get("error", ""))

    def test_auth_required(self):
        r = self._post("/alarms", {"prompt": "anything"}, token="")
        self.assertEqual(r.status_code, 401)

    def test_list_alarms_returns_created(self):
        # create one then list
        self._post("/alarms", {"prompt": "wake me when ETH goes above $4000"})
        r = self._get("/alarms")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("alarms", data)
        self.assertTrue(any(
            "ETH" in a["condition"] or "eth" in a["condition"].lower()
            for a in data["alarms"]
        ))

    def test_caller_override_source_hint(self):
        r = self._post("/alarms", {
            "prompt": "something custom",
            "source_hint": "news",
            "poll_seconds": 90,
        })
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["alarm"]["source_hint"], "news")
        self.assertEqual(body["alarm"]["poll_seconds"], 90)

    def test_cancel_alarm(self):
        created = self._post("/alarms", {"prompt": "BTC above 100k"}).json()
        aid = created["alarm"]["id"]
        r = self._post("/alarms/cancel", {"alarm_id": aid})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])


if __name__ == "__main__":
    unittest.main()
