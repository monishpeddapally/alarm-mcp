"""Data source plugins.

Each source returns a small text 'snapshot' describing current world state
relevant to an alarm condition. The LLM evaluator decides whether the
condition is satisfied given the snapshot.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

import httpx


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


async def _get_json(client: httpx.AsyncClient, url: str) -> Optional[dict]:
    try:
        r = await client.get(url, timeout=10, headers={"User-Agent": USER_AGENT})
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Cricket
# ---------------------------------------------------------------------------

# Public Cricbuzz JSON endpoints (used by their website). No key required.
CRIC_LIVE = "https://www.cricbuzz.com/api/cricket-match/commentary-info/{mid}"
CRIC_LIVE_MATCHES = "https://www.cricbuzz.com/api/cricket-match/live-matches"
CRIC_COMMENTARY = "https://www.cricbuzz.com/api/html/cricket-scorecard/{mid}"

# Open-source mirror that proxies cricbuzz — good fallback if cloudflare blocks us.
# Self-host or use the public deployment.
CRIC_MIRROR_LIVE = "https://cricbuzz-live.vercel.app/v1/matches/live"
CRIC_MIRROR_SCORE = "https://cricbuzz-live.vercel.app/v1/score/{mid}"


async def cricket_snapshot(condition: str) -> str:
    """Return a compact text snapshot of every currently live international/league match.

    Tries the direct Cricbuzz JSON endpoints first; if Cloudflare blocks the
    request, falls back to the public ``cricbuzz-live`` mirror which exposes the
    same data through a simpler API.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Primary: direct cricbuzz
        live = await _get_json(client, CRIC_LIVE_MATCHES)
        if live:
            match_ids = _extract_live_match_ids(live)
            if not match_ids:
                return "[cricket] no live matches right now"
            snaps = await asyncio.gather(*[_one_match(client, mid) for mid in match_ids[:6]])
            joined = "\n\n".join(s for s in snaps if s)
            if joined:
                return joined

        # Fallback: mirror
        mirror = await _get_json(client, CRIC_MIRROR_LIVE)
        if not mirror:
            return "[cricket] both cricbuzz and mirror unreachable"
        matches = (mirror.get("data") or {}).get("matchList") or mirror.get("data") or []
        if isinstance(matches, dict):
            matches = matches.get("typeMatches") or []
        ids: list[str] = []
        for m in matches:
            mid = m.get("matchId") or m.get("id") or m.get("match_id")
            if mid:
                ids.append(str(mid))
        if not ids:
            return "[cricket] no live matches found via mirror"
        snaps = await asyncio.gather(*[_mirror_one_match(client, mid) for mid in ids[:6]])
        return "\n\n".join(s for s in snaps if s) or "[cricket] mirror returned no detail"


async def _mirror_one_match(client: httpx.AsyncClient, mid: str) -> str:
    data = await _get_json(client, CRIC_MIRROR_SCORE.format(mid=mid))
    if not data:
        return ""
    d = data.get("data") or {}
    return (
        f"[match {mid}] {d.get('title','?')}\n"
        f"  update: {d.get('update','')}\n"
        f"  score: {d.get('liveScore','')} RR={d.get('runRate','')}\n"
        f"  striker: {d.get('batsmanOne','-')} {d.get('batsmanOneRun','-')}{d.get('batsmanOneBall','')}\n"
        f"  non-striker: {d.get('batsmanTwo','-')} {d.get('batsmanTwoRun','-')}{d.get('batsmanTwoBall','')}\n"
        f"  bowler: {d.get('bowlerOne','-')} {d.get('bowlerOneOver','')}-{d.get('bowlerOneRun','')}-{d.get('bowlerOneWickets','')}"
    )


def _extract_live_match_ids(payload: dict) -> list[str]:
    ids: list[str] = []
    # cricbuzz wraps matches under typeMatches -> seriesMatches -> matches
    for tm in payload.get("typeMatches", []) or []:
        for sm in tm.get("seriesMatches", []) or []:
            wrap = sm.get("seriesAdWrapper") or {}
            for m in wrap.get("matches", []) or []:
                info = m.get("matchInfo") or {}
                if info.get("state", "").lower() in ("in progress", "innings break", "tea", "lunch", "rain", "stumps"):
                    if info.get("matchId"):
                        ids.append(str(info["matchId"]))
    return ids


async def _one_match(client: httpx.AsyncClient, mid: str) -> str:
    data = await _get_json(client, CRIC_LIVE.format(mid=mid))
    if not data:
        return ""
    mh = data.get("matchHeader") or {}
    mss = data.get("miniscore") or {}
    teams = f"{mh.get('team1', {}).get('shortName','?')} vs {mh.get('team2', {}).get('shortName','?')}"
    status = mh.get("status") or mss.get("status") or ""
    bats = mss.get("batsmanStriker") or {}
    bats2 = mss.get("batsmanNonStriker") or {}
    bowl = mss.get("bowlerStriker") or {}
    inn = mss.get("matchScoreDetails", {}).get("inningsScoreList", [])

    score_lines = []
    for ing in inn:
        score_lines.append(
            f"  {ing.get('batTeamName','?')} {ing.get('score','?')}/{ing.get('wickets','?')} "
            f"in {ing.get('overs','?')} ov (inns {ing.get('inningsId','?')})"
        )

    # last few commentary lines if present
    comm_lines = []
    for c in (data.get("commentaryList") or [])[:6]:
        text = c.get("commText") or ""
        # strip cricbuzz inline tags like B0$, B1$, etc.
        text = re.sub(r"[A-Z]\d?\$[^|]*\|", "", text)
        if text.strip():
            comm_lines.append(f"    • {text.strip()[:200]}")

    return (
        f"[match {mid}] {teams} — {status}\n"
        f"  striker: {bats.get('batName','-')} {bats.get('batRuns','-')}({bats.get('batBalls','-')})\n"
        f"  non-striker: {bats2.get('batName','-')} {bats2.get('batRuns','-')}({bats2.get('batBalls','-')})\n"
        f"  bowler: {bowl.get('bowlName','-')} {bowl.get('bowlOvs','-')}-{bowl.get('bowlMaidens','-')}-"
        f"{bowl.get('bowlRuns','-')}-{bowl.get('bowlWkts','-')}\n"
        + "\n".join(score_lines)
        + ("\n  recent:\n" + "\n".join(comm_lines) if comm_lines else "")
    )


# ---------------------------------------------------------------------------
# Generic web / news source — uses DuckDuckGo HTML (no key) for breaking news
# ---------------------------------------------------------------------------

DDG = "https://duckduckgo.com/html/?q={q}&kl=us-en"


async def generic_snapshot(condition: str) -> str:
    """Fetch a few fresh web snippets for any non-cricket condition."""
    q = condition + " latest news"
    url = DDG.format(q=httpx.QueryParams({"q": q}).get("q") or q)
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.get(url, timeout=10, headers={"User-Agent": USER_AGENT})
            html = r.text
    except Exception as e:
        return f"[web] fetch error: {e}"

    # crude: pull first ~8 result snippets out of DDG HTML
    snippets = re.findall(
        r'class="result__snippet"[^>]*>(.*?)</a>',
        html,
        flags=re.S,
    )
    cleaned = []
    for s in snippets[:8]:
        text = re.sub(r"<[^>]+>", "", s).strip()
        if text:
            cleaned.append("• " + text[:280])
    if not cleaned:
        return f"[web] no snippets for: {condition}"
    return f"[web snapshot for: {condition}]\n" + "\n".join(cleaned)


# ---------------------------------------------------------------------------
# Crypto/stock price — Coingecko (no key)
# ---------------------------------------------------------------------------

COINGECKO = "https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"


async def price_snapshot(asset_ids: list[str]) -> str:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                COINGECKO.format(ids=",".join(asset_ids)),
                timeout=10,
                headers={"User-Agent": USER_AGENT},
            )
            data = r.json()
    except Exception as e:
        return f"[price] error: {e}"
    if not data:
        return "[price] no data"
    lines = [f"{k}: ${v.get('usd', '?')}" for k, v in data.items()]
    return "[price snapshot]\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

CRICKET_HINTS = (
    "bat", "batting", "wicket", "bowl", "innings", "over", "boundary", "six", "four",
    "century", "fifty", "stump", "lbw", "catch", "ipl", "odi", "t20", "test", "cricket",
)
PRICE_HINTS = ("btc", "bitcoin", "eth", "ethereum", "sol", "solana", "doge", "crypto", "price")


async def fetch_snapshot(condition: str, source_hint: Optional[str] = None) -> str:
    hint = (source_hint or "").lower()
    c = condition.lower()

    if hint == "cricket" or any(h in c for h in CRICKET_HINTS):
        return await cricket_snapshot(condition)

    if hint.startswith("price:") or any(h in c for h in PRICE_HINTS):
        ids = []
        if hint.startswith("price:"):
            ids = [hint.split(":", 1)[1].strip()]
        else:
            for tag, gid in (("bitcoin", "bitcoin"), ("btc", "bitcoin"),
                             ("ethereum", "ethereum"), ("eth", "ethereum"),
                             ("solana", "solana"), ("sol", "solana"),
                             ("doge", "dogecoin")):
                if tag in c and gid not in ids:
                    ids.append(gid)
        if not ids:
            ids = ["bitcoin"]
        return await price_snapshot(ids)

    return await generic_snapshot(condition)
