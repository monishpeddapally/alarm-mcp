"""Classify a natural-language alarm prompt into a normalized trigger skeleton.

This is intentionally a small, deterministic, rules-based classifier — it
does not call any LLM or paid API. Its job is to produce a *skeleton* the
rest of the system (and the iOS client) can reason about:

    {
      "category": "cricket" | "price" | "live_event" | "unknown",
      "source_hint": "cricket" | "price:bitcoin" | "news" | None,
      "supported": bool,
      "params": { ... category-specific best-effort parse ... },
      "notes": str,
    }

If `supported` is False, the alarm is still accepted and stored, but the
client knows it will be evaluated via the generic web/news fallback and may
not fire reliably. This avoids pretending we have world-event monitoring
that doesn't exist.

Keep this file free of any third-party dependencies beyond stdlib so it is
trivial to unit test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional


CRICKET_KEYWORDS = (
    "bat", "batting", "comes to bat", "wicket", "bowl", "innings", "over",
    "boundary", "six", "four", "century", "fifty", "stump", "lbw", "catch",
    "ipl", "odi", "t20", "test match", "cricket", "wickets",
)

PRICE_COINS = {
    "btc": "bitcoin",
    "bitcoin": "bitcoin",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "sol": "solana",
    "solana": "solana",
    "doge": "dogecoin",
    "dogecoin": "dogecoin",
}

LIVE_EVENT_KEYWORDS = (
    "speaking", "speech", "press conference", "live", "goes live",
    "starts speaking", "begins speaking", "takes the stage", "addresses",
    "interview", "announces", "broadcast",
)

PRICE_DIRECTION = {
    "falls": "below",
    "drops": "below",
    "goes below": "below",
    "below": "below",
    "under": "below",
    "rises": "above",
    "goes above": "above",
    "above": "above",
    "over": "above",
    "hits": "hits",
    "reaches": "hits",
    "crosses": "above",
}


@dataclass
class Trigger:
    category: str  # "cricket" | "price" | "live_event" | "unknown"
    source_hint: Optional[str]
    supported: bool
    params: dict = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _extract_proper_nouns(text: str) -> list[str]:
    return re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", text)


def _classify_cricket(prompt: str, low: str) -> Optional[Trigger]:
    if not any(kw in low for kw in CRICKET_KEYWORDS):
        return None
    names = _extract_proper_nouns(prompt)
    # Strip generic terms accidentally capitalised by the user
    names = [n for n in names if n.lower() not in ("india", "australia", "england",
                                                    "pakistan", "south", "africa",
                                                    "new", "zealand", "sri", "lanka",
                                                    "west", "indies", "bangladesh",
                                                    "afghanistan", "ireland")]
    event = None
    if "comes to bat" in low or "comes in to bat" in low or "starts batting" in low:
        event = "batsman_arrives"
    elif "out" in low or "wicket" in low:
        event = "wicket"
    elif "century" in low:
        event = "century"
    elif "fifty" in low or "half-century" in low:
        event = "fifty"
    return Trigger(
        category="cricket",
        source_hint="cricket",
        supported=True,
        params={"event": event, "player": names[0] if names else None,
                "all_players": names},
        notes="Routed to cricbuzz live snapshot; evaluator decides when condition fires.",
    )


_PRICE_NUMBER_RE = re.compile(
    r"\$?\s*([0-9]+(?:[,.][0-9]+)*)\s*(k|m|b|usd|dollars)?",
    re.IGNORECASE,
)


def _parse_price(text: str) -> Optional[float]:
    m = _PRICE_NUMBER_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        val = float(raw)
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        val *= 1_000
    elif suffix == "m":
        val *= 1_000_000
    elif suffix == "b":
        val *= 1_000_000_000
    return val


def _classify_price(prompt: str, low: str) -> Optional[Trigger]:
    asset = None
    for tag, gid in PRICE_COINS.items():
        if re.search(rf"\b{re.escape(tag)}\b", low):
            asset = gid
            break
    if asset is None:
        return None

    direction = None
    for phrase, dirn in PRICE_DIRECTION.items():
        if phrase in low:
            direction = dirn
            break

    threshold = _parse_price(low)

    return Trigger(
        category="price",
        source_hint=f"price:{asset}",
        supported=True,
        params={
            "asset": asset,
            "direction": direction,
            "threshold_usd": threshold,
        },
        notes="Routed to coingecko price snapshot; evaluator compares against threshold.",
    )


def _classify_live_event(prompt: str, low: str) -> Optional[Trigger]:
    if not any(kw in low for kw in LIVE_EVENT_KEYWORDS):
        return None
    subjects = _extract_proper_nouns(prompt)
    return Trigger(
        category="live_event",
        source_hint="news",
        # We mark supported=True because the news fallback CAN sometimes catch
        # a "live now" headline — but warn that latency/accuracy is poor.
        supported=True,
        params={"subject": subjects[0] if subjects else None,
                "all_subjects": subjects},
        notes=(
            "Routed to generic web snapshot. Detection lag can be minutes; "
            "may miss events that don't surface in news quickly. A dedicated "
            "live-event worker (YouTube/Twitch/news API) is needed for "
            "reliable second-level firing."
        ),
    )


def classify(prompt: str) -> Trigger:
    """Best-effort classification of a natural-language alarm prompt."""
    if not prompt or not prompt.strip():
        return Trigger(
            category="unknown",
            source_hint=None,
            supported=False,
            params={},
            notes="Empty prompt.",
        )

    low = _normalize(prompt)

    for fn in (_classify_cricket, _classify_price, _classify_live_event):
        t = fn(prompt, low)
        if t is not None:
            return t

    return Trigger(
        category="unknown",
        source_hint=None,
        supported=False,
        params={},
        notes=(
            "No known category matched. The alarm will still be stored and "
            "checked against a generic web snapshot, but accuracy depends "
            "entirely on the LLM evaluator and may not fire reliably."
        ),
    )


def suggest_poll_seconds(trigger: Trigger) -> int:
    """Reasonable default polling cadence per category."""
    if trigger.category == "cricket":
        return 20
    if trigger.category == "price":
        return 60
    if trigger.category == "live_event":
        return 120
    return 180
