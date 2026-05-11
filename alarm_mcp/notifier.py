"""Fire the actual alarm when a trigger condition is met.

Channels (any combination, configured via env vars):

  1. macOS native:
        - plays a loud system sound on repeat for ALARM_MCP_RING_SECONDS
        - sends a banner notification via `osascript`
     Always on by default on darwin.

  2. Apple Shortcuts (real Alarm app integration):
        Set ALARM_MCP_SHORTCUT="Fire Alarm MCP" to the name of a Shortcut
        you've created in the Shortcuts app. The shortcut will be run with
        the alarm label as text input — your Shortcut can then "Create
        Alarm" or "Set Timer" using that input. This is the official way to
        touch the system Alarm/Clock app.

  3. ntfy.sh push (works anywhere, rings phone):
        Set ALARM_MCP_NTFY_TOPIC="your-secret-topic" and install ntfy on
        your phone subscribed to that topic.

  4. Generic webhook:
        Set ALARM_MCP_WEBHOOK="https://..." — POSTed a JSON payload.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import httpx

from .models import Alarm


DEFAULT_RING_SECONDS = int(os.environ.get("ALARM_MCP_RING_SECONDS", "20"))


async def fire(alarm: Alarm, evidence: str = "") -> list[str]:
    """Fire all configured channels. Returns list of channels actually used."""
    used: list[str] = []
    tasks = []

    if platform.system() == "Darwin":
        tasks.append(("macos_sound", _ring_macos(alarm, evidence)))
        tasks.append(("macos_banner", _banner_macos(alarm, evidence)))

    if shortcut := os.environ.get("ALARM_MCP_SHORTCUT"):
        tasks.append(("shortcut", _run_shortcut(shortcut, alarm, evidence)))

    if topic := os.environ.get("ALARM_MCP_NTFY_TOPIC"):
        tasks.append(("ntfy", _ntfy(topic, alarm, evidence)))

    if webhook := os.environ.get("ALARM_MCP_WEBHOOK"):
        tasks.append(("webhook", _webhook(webhook, alarm, evidence)))

    if not tasks:
        # last-resort: print loudly to stderr so the user at least sees something
        print(f"\a\n*** ALARM FIRED *** {alarm.label or alarm.condition}\n{evidence}\n",
              flush=True)
        return ["stderr"]

    results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
    for (name, _), res in zip(tasks, results):
        if not isinstance(res, Exception):
            used.append(name)
    return used


# --- macOS native ----------------------------------------------------------

async def _ring_macos(alarm: Alarm, evidence: str) -> None:
    """Play a loud system sound repeatedly for DEFAULT_RING_SECONDS."""
    sounds_dir = Path("/System/Library/Sounds")
    candidates = ["Sosumi.aiff", "Submarine.aiff", "Glass.aiff", "Ping.aiff"]
    sound = next((sounds_dir / c for c in candidates if (sounds_dir / c).exists()), None)
    if not sound or not shutil.which("afplay"):
        return

    deadline = asyncio.get_event_loop().time() + DEFAULT_RING_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        proc = await asyncio.create_subprocess_exec(
            "afplay", "-v", "2", str(sound),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()


async def _banner_macos(alarm: Alarm, evidence: str) -> None:
    if not shutil.which("osascript"):
        return
    title = alarm.label or "Alarm MCP"
    body = (alarm.condition + ("\n" + evidence if evidence else ""))[:300].replace('"', "'")
    # display notification + display dialog (dialog forces user acknowledgement)
    script = (
        f'display notification "{body}" with title "{title}" sound name "Sosumi"'
    )
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


# --- Apple Shortcuts (the bridge to the real Alarm app) --------------------

async def _run_shortcut(shortcut_name: str, alarm: Alarm, evidence: str) -> None:
    """Invoke a named Shortcut, passing alarm context as stdin text."""
    if not shutil.which("shortcuts"):
        return
    payload = json.dumps({
        "label": alarm.label or alarm.condition,
        "condition": alarm.condition,
        "evidence": evidence,
        "id": alarm.id,
    })
    proc = await asyncio.create_subprocess_exec(
        "shortcuts", "run", shortcut_name, "--input-path", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate(payload.encode())


# --- ntfy.sh push (rings the user's phone) ---------------------------------

async def _ntfy(topic: str, alarm: Alarm, evidence: str) -> None:
    url = f"https://ntfy.sh/{topic}"
    headers = {
        "Title": (alarm.label or "Alarm MCP")[:200],
        "Priority": "urgent",
        "Tags": "rotating_light,bell",
    }
    body = f"{alarm.condition}\n\n{evidence}".strip()
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, headers=headers, content=body.encode())


# --- Generic webhook -------------------------------------------------------

async def _webhook(url: str, alarm: Alarm, evidence: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, json={
            "alarm_id": alarm.id,
            "label": alarm.label,
            "condition": alarm.condition,
            "evidence": evidence,
            "triggered_at": alarm.triggered_at,
        })
