"""Decide whether an alarm condition is satisfied by a data snapshot.

Uses Anthropic Claude by default (ANTHROPIC_API_KEY). Falls back to a tiny
keyword heuristic if no key is configured, so the server still functions.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class Verdict:
    fired: bool
    confidence: float
    evidence: str
    summary: str


SYSTEM = """You are an alarm-trigger evaluator. Given a user CONDITION (in
natural language) and a DATA SNAPSHOT pulled from a live source, decide
whether the condition is currently TRUE.

Be conservative: only fire when the snapshot clearly shows the condition has
just happened or is happening right now. Do NOT fire on speculation, on
"about to" wording, or on stale historical references.

Respond ONLY with strict JSON of the form:
{"fired": true|false, "confidence": 0.0-1.0, "evidence": "<≤30 word quote from snapshot>", "summary": "<≤25 word reason>"}
"""


async def evaluate(condition: str, snapshot: str) -> Verdict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("ALARM_MCP_MODEL", "claude-3-5-haiku-latest")

    if api_key:
        try:
            return await _evaluate_claude(condition, snapshot, api_key, model)
        except Exception as e:
            # fall back rather than crash the polling loop
            return _heuristic(condition, snapshot, fallback_reason=f"llm error: {e}")

    return _heuristic(condition, snapshot, fallback_reason="no ANTHROPIC_API_KEY set")


async def _evaluate_claude(
    condition: str, snapshot: str, api_key: str, model: str
) -> Verdict:
    # Lazy import so the module loads even without anthropic installed
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)
    msg = await client.messages.create(
        model=model,
        max_tokens=300,
        system=SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"CONDITION:\n{condition}\n\n"
                    f"DATA SNAPSHOT:\n{snapshot[:8000]}\n\n"
                    "Respond with JSON only."
                ),
            }
        ],
    )
    text = "".join(getattr(b, "text", "") for b in msg.content).strip()
    # tolerate code fences
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # try to grab the last JSON object in the text
        m = re.search(r"\{.*\}", text, flags=re.S)
        data = json.loads(m.group(0)) if m else {}
    return Verdict(
        fired=bool(data.get("fired")),
        confidence=float(data.get("confidence", 0.0)),
        evidence=str(data.get("evidence", ""))[:400],
        summary=str(data.get("summary", ""))[:400],
    )


def _heuristic(condition: str, snapshot: str, fallback_reason: str = "") -> Verdict:
    """Very crude fallback so the server stays useful without an LLM key.

    Strategy: extract the key noun phrases from the condition (capitalised
    tokens + a few keywords) and require at least one strong match in the
    snapshot. Good enough for 'Rishabh Pant comes to bat' style conditions
    where the snapshot literally contains the striker's name.
    """
    cond = condition.lower()
    snap = snapshot.lower()

    # capture proper nouns from the original (case-preserving) condition
    proper_nouns = re.findall(r"\b[A-Z][a-z]+\b", condition)
    name_hit = any(p.lower() in snap for p in proper_nouns) if proper_nouns else False

    keywords = []
    for kw in ("bat", "wicket", "century", "fifty", "six", "four", "out"):
        if kw in cond:
            keywords.append(kw)
    kw_hit = any(k in snap for k in keywords) if keywords else True

    fired = bool(name_hit and kw_hit) if proper_nouns else False

    return Verdict(
        fired=fired,
        confidence=0.6 if fired else 0.0,
        evidence=("matched: " + ", ".join(proper_nouns)) if fired else "",
        summary=f"heuristic fallback ({fallback_reason})" if fallback_reason else "heuristic fallback",
    )
