from __future__ import annotations

import json
import os
import math
import time
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Callable, Iterable

from backend.technicals import encode_image_to_data_url
from backend.orats_client import OratsClient
from backend.benzinga_client import BenzingaClient, BenzingaResponse

LOG = logging.getLogger(__name__)


def _pick(d: dict, keys: List[str]) -> dict:
    out = {}
    for k in keys:
        if k in d:
            out[k] = d.get(k)
    return out


def _content_to_text(content: Any) -> str:
    """
    Normalize OpenAI message content to plain text.
    Some SDKs/models return `message.content` as a list of parts instead of a string.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # list-of-parts (dicts or objects with `.text`)
    if isinstance(content, list):
        parts: List[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
                continue
            if isinstance(p, dict):
                # common shapes: {"type":"text","text":"..."} or {"text":"..."}
                t = p.get("text")
                if isinstance(t, str):
                    parts.append(t)
                continue
            # fallback for SDK objects
            t = getattr(p, "text", None)
            if isinstance(t, str):
                parts.append(t)
        return "\n".join([x for x in parts if x is not None])
    # fallback for objects with `.text`
    t = getattr(content, "text", None)
    if isinstance(t, str):
        return t
    return ""


def _env_bool(name: str, default: bool = False) -> bool:
    v = str(os.getenv(name) or "").strip().lower()
    if not v:
        return bool(default)
    return v in ("1", "true", "yes", "y", "on")


def _truncate(s: Any, n: int) -> str:
    t = str(s or "").replace("\n", " ").strip()
    return (t[:n] + "…") if len(t) > n else t


def _wants_news_or_gap(question: str) -> bool:
    q = str(question or "").lower()
    keys = (
        "news",
        "headline",
        "headlines",
        "pre-market",
        "premarket",
        "gap",
        "gapping",
        "catalyst",
        "catalysts",
        "what can move",
        "what can gap",
        "drivers",
    )
    return any(k in q for k in keys)

def _budget_profile(name: str) -> dict:
    p = str(name or "").strip().lower() or "default"
    if p == "tight":
        return {"max_tool_calls": 3, "max_steps": 4, "wall_s": 10.0, "max_bytes_per_tool": 30_000}
    if p == "loose":
        return {"max_tool_calls": 12, "max_steps": 10, "wall_s": 45.0, "max_bytes_per_tool": 120_000}
    return {"max_tool_calls": 6, "max_steps": 6, "wall_s": 20.0, "max_bytes_per_tool": 60_000}


def _json_dumps_safe(obj: Any, max_len: int) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        s = str(obj)
    if len(s) > int(max_len):
        s = s[: int(max_len)] + "…"
    return s


def _parse_json_maybe(x: Any) -> dict:
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else {"value": v}
        except Exception:
            return {"value": s}
    return {}


def _extract_tool_calls_from_response(resp: Any) -> List[dict]:
    """
    Robustly extract tool/function calls from OpenAI Responses API output across SDK versions.
    Returns a list of dicts: {\"id\":..., \"name\":..., \"arguments\":{...}}
    """
    calls: List[dict] = []
    try:
        raw = resp.model_dump() if hasattr(resp, "model_dump") else {}
    except Exception:
        raw = {}

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, dict):
            t = node.get("type")
            # common shapes:
            # - {type:\"function_call\", name:\"...\", arguments:\"{...}\", call_id:\"...\"}
            # - {type:\"tool_call\", name:\"...\", arguments:{...}, id:\"...\"}
            if t in ("function_call", "tool_call"):
                fn = node.get("function") if isinstance(node.get("function"), dict) else {}
                name = node.get("name") or fn.get("name")
                args = node.get("arguments") or fn.get("arguments")
                call_id = node.get("call_id") or node.get("id") or node.get("tool_call_id")
                if isinstance(args, str):
                    args_d = _parse_json_maybe(args)
                elif isinstance(args, dict):
                    args_d = args
                else:
                    args_d = {}
                if isinstance(name, str) and name:
                    calls.append({"id": str(call_id or ""), "name": str(name), "arguments": args_d})
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(raw.get("output") or raw)
    # de-dupe by (id,name)
    seen = set()
    out = []
    for c in calls:
        k = (c.get("id") or "", c.get("name") or "")
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def _tool_schema_orats_get_live_spot() -> dict:
    # Responses API function tool format:
    # {"type":"function","function":{"name":...,"description":...,"parameters":...}}
    return {
        "type": "function",
        "function": {
            "name": "orats_get_live_spot",
            "description": "Fetch best-effort live spot/stock price for ticker from ORATS Live summaries.",
            "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]},
        },
    }


def _tool_schema_orats_get_expirations() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "orats_get_expirations",
            "description": "Fetch available option expirations for ticker from ORATS Live expirations (fallback: infer from live strikes).",
            "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]},
        },
    }


def _tool_schema_orats_get_chain_slice() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "orats_get_chain_slice",
            "description": "Fetch an options chain slice for a ticker+expiry from ORATS Live strikes and summarize OI/volume/gamma walls within a strike range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "expiry": {"type": "string", "description": "YYYY-MM-DD"},
                    "strike_min": {"type": "number"},
                    "strike_max": {"type": "number"},
                    "target_strikes": {"type": "array", "items": {"type": "number"}, "description": "Optional strikes to highlight"},
                },
                "required": ["ticker", "expiry", "strike_min", "strike_max"],
            },
        },
    }


def _tool_schema_benzinga_get_news() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "benzinga_get_news",
            "description": "Fetch recent Benzinga headlines for tickers/topics/date window and return a compact list with URLs/snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tickers": {"type": "string", "description": "Comma-separated symbols"},
                    "topics": {"type": "string", "description": "Optional topics filter"},
                    "days": {"type": "number", "description": "Lookback days (default 2)"},
                    "limit": {"type": "number", "description": "Max items (default 12)"},
                },
                "required": [],
            },
        },
    }


def _tool_schema_web_fetch() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a public URL (no logins), extract readable text snippet and title. Use for public pages (including X/Reddit pages that are accessible).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "number", "description": "Max chars of extracted text (default 6000)"},
                },
                "required": ["url"],
            },
        },
    }


def _orats_live_spot(client: OratsClient, *, ticker: str) -> dict:
    t = str(ticker or "").strip().upper()
    if not t:
        return {"ok": False, "error": "ticker required"}
    try:
        rows = client.live_summaries(ticker=t).rows or []
        row = next((x for x in rows if isinstance(x, dict)), None) or {}
        spot = row.get("spotPrice")
        px = spot if spot not in (None, "", 0) else row.get("stockPrice")
        return {"ok": True, "ticker": t, "spotPrice": spot, "stockPrice": row.get("stockPrice"), "price": px, "row": _pick(row, ["tradeDate", "spotPrice", "stockPrice"])}
    except Exception as e:
        return {"ok": False, "ticker": t, "error": f"{type(e).__name__}: {e}"}


def _infer_expiries_from_strikes(rows: List[dict]) -> List[str]:
    out: List[str] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        d = r.get("expirDate") or r.get("expiry") or r.get("expDate") or r.get("exp_date")
        if not d:
            continue
        ds = str(d)[:10]
        if len(ds) == 10 and ds not in out:
            out.append(ds)
    out.sort()
    return out


def _orats_expirations(client: OratsClient, *, ticker: str) -> dict:
    t = str(ticker or "").strip().upper()
    if not t:
        return {"ok": False, "error": "ticker required"}
    try:
        if callable(getattr(client, "live_expirations", None)):
            rows = client.live_expirations(ticker=t).rows or []
            exps = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                d = r.get("expirDate") or r.get("expiry") or r.get("expDate")
                if d:
                    exps.append(str(d)[:10])
            exps = sorted(list(dict.fromkeys([x for x in exps if x and len(x) == 10])))
            if exps:
                return {"ok": True, "ticker": t, "expirations": exps, "source": "live_expirations"}
    except Exception:
        pass
    # fallback: infer from strikes
    try:
        fields = "ticker,tradeDate,expirDate,expiry,expDate,exp_date,strike,spotPrice,stockPrice,callOpenInterest,putOpenInterest,callVolume,putVolume"
        rows2 = client.live_strikes(ticker=t, fields=fields).rows or []
        exps2 = _infer_expiries_from_strikes([x for x in rows2 if isinstance(x, dict)])
        return {"ok": True, "ticker": t, "expirations": exps2, "source": "live_strikes_infer"}
    except Exception as e:
        return {"ok": False, "ticker": t, "error": f"{type(e).__name__}: {e}"}


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        if not math.isfinite(f):
            return None
        return f
    except Exception:
        return None


def _orats_chain_slice(
    client: OratsClient,
    *,
    ticker: str,
    expiry: str,
    strike_min: float,
    strike_max: float,
    target_strikes: Optional[List[float]] = None,
) -> dict:
    t = str(ticker or "").strip().upper()
    exp = str(expiry or "").strip()[:10]
    lo = float(strike_min)
    hi = float(strike_max)
    if not t or not exp:
        return {"ok": False, "error": "ticker and expiry required"}
    if hi < lo:
        lo, hi = hi, lo

    fields = ",".join(
        [
            "ticker",
            "tradeDate",
            "expirDate",
            "strike",
            "spotPrice",
            "stockPrice",
            "gamma",
            "callOpenInterest",
            "putOpenInterest",
            "callVolume",
            "putVolume",
            "callDelta",
            "putDelta",
            "callMidIv",
            "putMidIv",
        ]
    )
    warnings: List[str] = []
    try:
        rows = client.live_strikes_by_expiry(ticker=t, expiry=exp, fields=fields).rows or []
    except Exception as e:
        warnings.append(f"live_strikes_by_expiry failed: {type(e).__name__}: {e}")
        try:
            rows = client.live_strikes(ticker=t, fields=fields).rows or []
            rows = [r for r in rows if isinstance(r, dict) and str(r.get('expirDate') or r.get('expiry') or '')[:10] == exp]
            warnings.append("fell back to live_strikes filtered by expiry")
        except Exception as e2:
            return {"ok": False, "ticker": t, "expiry": exp, "error": f"{type(e2).__name__}: {e2}", "warnings": warnings}

    chain = [r for r in rows if isinstance(r, dict)]
    filt = []
    spot = None
    for r in chain:
        k = _to_float(r.get("strike"))
        if k is None:
            continue
        if not (lo <= float(k) <= hi):
            continue
        filt.append(r)
        if spot is None:
            spot = _to_float(r.get("spotPrice")) or _to_float(r.get("stockPrice"))
    if not filt:
        return {"ok": True, "ticker": t, "expiry": exp, "spot": spot, "range": {"lo": lo, "hi": hi}, "rowsUsed": 0, "warnings": warnings, "notes": ["No chain rows in strike range (check range/expiry)."]}

    def top_n(key: str, n: int = 8) -> List[dict]:
        pairs = []
        for r in filt:
            k = _to_float(r.get("strike"))
            v = _to_float(r.get(key))
            if k is None or v is None:
                continue
            pairs.append((float(v), float(k)))
        pairs.sort(reverse=True)
        out = [{"strike": int(k), key: v} for (v, k) in pairs[:n]]
        return out

    top_call_oi = top_n("callOpenInterest", 10)
    top_put_oi = top_n("putOpenInterest", 10)
    top_call_vol = top_n("callVolume", 8)
    top_put_vol = top_n("putVolume", 8)
    top_gamma = top_n("gamma", 10)

    # Sample rows near target strikes or around spot.
    targets = [float(x) for x in (target_strikes or []) if x is not None]
    sample: List[dict] = []
    def row_obj(r: dict) -> dict:
        return {
            "strike": _to_float(r.get("strike")),
            "callDelta": _to_float(r.get("callDelta")),
            "putDelta": _to_float(r.get("putDelta")),
            "callIv": _to_float(r.get("callMidIv")),
            "putIv": _to_float(r.get("putMidIv")),
            "callOI": _to_float(r.get("callOpenInterest")),
            "putOI": _to_float(r.get("putOpenInterest")),
            "callVol": _to_float(r.get("callVolume")),
            "putVol": _to_float(r.get("putVolume")),
            "gamma": _to_float(r.get("gamma")),
        }

    if targets:
        for tgt in targets:
            near = sorted(filt, key=lambda r: abs((_to_float(r.get("strike")) or 0.0) - float(tgt)))[:3]
            for r in near:
                sample.append(row_obj(r))
    elif spot is not None:
        near = sorted(filt, key=lambda r: abs((_to_float(r.get("strike")) or 0.0) - float(spot)))[:10]
        sample = [row_obj(r) for r in near]

    return {
        "ok": True,
        "ticker": t,
        "expiry": exp,
        "spot": spot,
        "range": {"lo": lo, "hi": hi},
        "rowsUsed": int(len(filt)),
        "top": {
            "callOI": top_call_oi,
            "putOI": top_put_oi,
            "callVolume": top_call_vol,
            "putVolume": top_put_vol,
            "gamma": top_gamma,
        },
        "sample": sample[:20],
        "warnings": warnings,
        "notes": ["ORATS Live chain slice (best-effort). Field availability depends on entitlement."],
    }


def _benzinga_news(
    client: BenzingaClient,
    *,
    tickers: Optional[str] = None,
    topics: Optional[str] = None,
    days: int = 2,
    limit: int = 12,
) -> dict:
    now = dt.datetime.utcnow().date()
    date_to = now.isoformat()
    date_from = (now - dt.timedelta(days=max(1, int(days or 2)))).isoformat()
    lim = int(limit or 12)
    lim = max(1, min(lim, 20))
    try:
        resp: BenzingaResponse = client.news(tickers=tickers, topics=topics, date_from=date_from, date_to=date_to, page=0, page_size=50, sort="updated")
        rows = resp.rows or []
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    items = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = _truncate(r.get("title") or r.get("headline") or "", 240)
        if not title:
            continue
        items.append(
            {
                "title": title,
                "updated": _truncate(r.get("updated") or r.get("updated_at") or "", 40),
                "created": _truncate(r.get("created") or r.get("created_at") or r.get("published") or "", 40),
                "source": _truncate(r.get("source") or "", 40),
                "url": _truncate(r.get("url") or "", 300),
                "summary": _truncate(r.get("summary") or r.get("teaser") or "", 360),
                "tickers": _truncate(r.get("tickers") or r.get("symbols") or "", 120),
            }
        )
        if len(items) >= lim:
            break
    return {
        "ok": True,
        "provider": "benzinga",
        "window": {"from": date_from, "to": date_to},
        "tickers": tickers,
        "topics": topics,
        "items": items,
    }


def _web_fetch_public(*, url: str, max_chars: int = 6000, timeout_s: float = 12.0) -> dict:
    u = str(url or "").strip()
    if not u:
        return {"ok": False, "error": "url required"}
    if not (u.startswith("http://") or u.startswith("https://")):
        return {"ok": False, "error": "url must be http(s)"}
    max_c = int(max_chars or 6000)
    max_c = max(500, min(max_c, 20_000))
    try:
        import urllib.request
        import re

        req = urllib.request.Request(
            u,
            headers={"User-Agent": "RavenTech/AskRaven (public fetch)", "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:  # nosec - public URL fetch by design
            raw = resp.read(250_000) or b""
            txt = raw.decode("utf-8", errors="ignore")
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", txt, flags=re.IGNORECASE | re.DOTALL)
        if m:
            title = re.sub(r"\s+", " ", re.sub(r"<.*?>", " ", m.group(1))).strip()[:240]
        # crude HTML strip
        body = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\\1>", " ", txt)
        body = re.sub(r"(?is)<.*?>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
        return {"ok": True, "url": u, "title": title, "text": body[:max_c], "notes": ["Public web fetch; content may be truncated."]}
    except Exception as e:
        return {"ok": False, "url": u, "error": f"{type(e).__name__}: {e}"}


def askraven_agent_chat(
    *,
    question: str,
    context_pack: Dict[str, Any],
    images: List[UploadedImage],
    orats_client: Optional[OratsClient] = None,
    benzinga_client: Optional[BenzingaClient] = None,
) -> str:
    """
    ChatGPT-style agent loop: model can request tools (ORATS/Benzinga/Web) as needed,
    and we iterate until we get a final answer or budgets are hit.
    """
    api_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY on server.")

    from openai import OpenAI  # type: ignore

    client = OpenAI(api_key=api_key)
    model = str(os.getenv("OPENAI_MODEL") or "gpt-5.2").strip()
    max_out = int(float(os.getenv("ASKRAVEN_MAX_OUTPUT_TOKENS") or 1400))
    effort = str(os.getenv("OPENAI_REASONING_EFFORT") or "high").strip().lower()

    enable_web = _env_bool("ASKRAVEN_ENABLE_WEB", False)
    enable_orats = _env_bool("ASKRAVEN_ENABLE_ORATS_TOOLS", True)
    enable_bz = _env_bool("ASKRAVEN_ENABLE_BENZINGA_TOOLS", True)
    budget = _budget_profile(os.getenv("ASKRAVEN_BUDGET") or "loose")

    start = time.time()
    tool_calls_used = 0

    # Enrich with tradeBrief only (no automatic ORATS/Benzinga fetch).
    ctx = dict(context_pack or {})
    try:
        ctx["tradeBrief"] = build_trade_brief(question=question, context_pack=ctx)
    except Exception:
        ctx["tradeBrief"] = {"enabled": False, "notes": ["Failed to build tradeBrief."]}

    # If the user explicitly asks for a specific chain (e.g., “Jan 2 option chain”),
    # do a minimal, best-effort prefetch so the model has the data to reason over.
    # This is not “automatic”; it is triggered by explicit user request.
    ql = str(question or "").lower()
    if enable_orats and orats_client is not None and ("option chain" in ql or "chain" in ql or "jan" in ql):
        try:
            import re

            # Try to parse an explicit YYYY-MM-DD first.
            m = re.search(r"(\d{4}-\d{2}-\d{2})", ql)
            expiry = m.group(1) if m else None
            # crude month-name parse for Jan/Feb/Mar... (assume current/next year)
            if expiry is None and "jan" in ql:
                # choose Jan 2 (common weekly) if mentioned
                m2 = re.search(r"jan\s*(\d{1,2})", ql)
                day = int(m2.group(1)) if m2 else 2
                year = dt.datetime.utcnow().year
                # if we're already past Jan in this year, use next year
                if dt.datetime.utcnow().month > 1:
                    year = year + 1
                expiry = f"{year}-01-{day:02d}"

            # Determine spot for strike band.
            sym = "SPX"
            if isinstance(ctx.get("underlying"), dict):
                sym = str(ctx.get("underlying", {}).get("symbol") or "SPX")
            spot_resp = _orats_live_spot(orats_client, ticker=sym)
            spot = _to_float(spot_resp.get("price")) if isinstance(spot_resp, dict) else None
            if spot is None:
                tb = ctx.get("tradeBrief") if isinstance(ctx.get("tradeBrief"), dict) else {}
                spot = _to_float(tb.get("spot")) if isinstance(tb, dict) else None
            if spot is not None and math.isfinite(float(spot)):
                lo = float(spot) * 0.96
                hi = float(spot) * 1.04
            else:
                lo, hi = 0.0, 0.0

            if expiry and lo and hi:
                target = None
                tb = ctx.get("tradeBrief") if isinstance(ctx.get("tradeBrief"), dict) else {}
                parsed = tb.get("parsedTrade") if isinstance(tb.get("parsedTrade"), dict) else {}
                if isinstance(parsed.get("strikes"), list):
                    target = [float(x) for x in parsed.get("strikes") if x is not None]
                ctx["oratsChainPrefetch"] = _orats_chain_slice(
                    orats_client,
                    ticker=sym,
                    expiry=str(expiry),
                    strike_min=lo,
                    strike_max=hi,
                    target_strikes=target,
                )
        except Exception:
            pass

    ctx_txt = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"), indent=2)
    base_user_text = f"RavenTech context pack:\n{ctx_txt}\n\nUser question:\n{str(question or '').strip()}"

    # Responses API tools
    tools: List[dict] = []
    if enable_orats:
        tools.extend([_tool_schema_orats_get_live_spot(), _tool_schema_orats_get_expirations(), _tool_schema_orats_get_chain_slice()])
    if enable_bz:
        tools.append(_tool_schema_benzinga_get_news())
    tools.append(_tool_schema_web_fetch())
    if enable_web:
        # built-in tool (executed by OpenAI). Some SDKs use web_search_preview.
        tools.append({"type": "web_search"})

    sys_txt = ASKRAVEN_SYSTEM_PROMPT + "\n\nIMPORTANT: You may use tools if needed. When citing web/news, include URLs. Return a non-empty plain-text answer."

    prev_id = None
    pending_tool_results: List[dict] = []

    def budget_exhausted() -> bool:
        if (time.time() - start) > float(budget["wall_s"]):
            return True
        if tool_calls_used >= int(budget["max_tool_calls"]):
            return True
        return False

    # executor map
    def exec_tool(name: str, args: dict) -> dict:
        nonlocal tool_calls_used
        tool_calls_used += 1
        if name.startswith("orats_"):
            if orats_client is None:
                return {"ok": False, "error": "ORATS client not available on server."}
            if name == "orats_get_live_spot":
                return _orats_live_spot(orats_client, ticker=str(args.get("ticker") or ""))
            if name == "orats_get_expirations":
                return _orats_expirations(orats_client, ticker=str(args.get("ticker") or ""))
            if name == "orats_get_chain_slice":
                return _orats_chain_slice(
                    orats_client,
                    ticker=str(args.get("ticker") or ""),
                    expiry=str(args.get("expiry") or ""),
                    strike_min=float(args.get("strike_min") or 0),
                    strike_max=float(args.get("strike_max") or 0),
                    target_strikes=[float(x) for x in (args.get("target_strikes") or []) if x is not None] if isinstance(args.get("target_strikes"), list) else None,
                )
        if name.startswith("benzinga_"):
            if benzinga_client is None:
                return {"ok": False, "error": "Benzinga client not available on server."}
            if name == "benzinga_get_news":
                return _benzinga_news(
                    benzinga_client,
                    tickers=str(args.get("tickers") or "") or None,
                    topics=str(args.get("topics") or "") or None,
                    days=int(float(args.get("days") or 2)),
                    limit=int(float(args.get("limit") or 12)),
                )
        if name == "web_fetch":
            return _web_fetch_public(url=str(args.get("url") or ""), max_chars=int(float(args.get("max_chars") or 6000)))
        return {"ok": False, "error": f"Unknown tool: {name}"}

    for step in range(int(budget["max_steps"])):
        if budget_exhausted():
            return "AskRaven budget exhausted (time/tool limit). Try a narrower question or increase ASK_RAVEN_BUDGET."

        def _responses_create_with_tools(use_tools: List[dict]) -> Any:
            kwargs: Dict[str, Any] = {"model": model, "max_output_tokens": max_out, "tools": use_tools}
            # Only set reasoning effort if it looks valid; otherwise omit.
            if str(effort).lower() in ("auto", "low", "medium", "high"):
                kwargs["reasoning"] = {"effort": str(effort).lower()}

            if prev_id is None:
                kwargs["input"] = [
                    {"role": "system", "content": [{"type": "text", "text": sys_txt}]},
                    {"role": "user", "content": [{"type": "text", "text": base_user_text}]},
                ]
            else:
                kwargs["previous_response_id"] = prev_id
                kwargs["input"] = pending_tool_results

            return client.responses.create(**kwargs)

        try:
            resp = _responses_create_with_tools(tools)
        except Exception as e:
            # Common failure mode: wrong built-in web tool name. Retry once with preview.
            if enable_web:
                try:
                    tools2 = [t for t in tools if not (isinstance(t, dict) and t.get("type") == "web_search")]
                    tools2.append({"type": "web_search_preview"})
                    resp = _responses_create_with_tools(tools2)
                except Exception as e2:
                    LOG.warning("AskRaven agent: responses.create failed (tools): %s", e2)
                    return call_openai(question=question, context_pack=ctx, images=images)
            LOG.warning("AskRaven agent: responses.create failed: %s", e)
            # If Responses API is unavailable, fall back to the existing single-shot call.
            return call_openai(question=question, context_pack=ctx, images=images)

        prev_id = getattr(resp, "id", None) or getattr(resp, "response_id", None)
        out_txt = getattr(resp, "output_text", None)
        if isinstance(out_txt, str) and out_txt.strip():
            return out_txt.strip()

        # Execute function tools requested by the model (web_search runs inside OpenAI).
        tool_calls = _extract_tool_calls_from_response(resp)
        func_calls = [c for c in tool_calls if isinstance(c, dict) and str(c.get("name") or "").strip()]
        if not func_calls:
            # If no tool calls and no output text, degrade safely.
            return call_openai(question=question, context_pack=ctx, images=images)

        pending_tool_results = []
        for c in func_calls:
            if budget_exhausted():
                return "AskRaven budget exhausted while fetching context. Try narrowing the request."
            name = str(c.get("name") or "")
            args = c.get("arguments") if isinstance(c.get("arguments"), dict) else {}
            call_id = str(c.get("id") or "") or f"call_{step}_{name}"
            result = exec_tool(name, args)
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_call_id": call_id,
                    "output": _json_dumps_safe(result, int(budget["max_bytes_per_tool"])),
                }
            )

    return call_openai(question=question, context_pack=ctx, images=images)

def _parse_trade_from_prompt(question: str) -> Dict[str, Any]:
    """
    Best-effort extraction of trade structure from user prompt.
    """
    import re

    q = str(question or "").strip()
    ql = q.lower()
    # capture digits even when suffixed like "6955c" or "500p"
    nums = [int(n) for n in re.findall(r"(\d{3,5})(?:[cCpP])?", q)]
    strikes = [n for n in nums if 1000 <= n <= 20000]
    strikes = sorted(list(dict.fromkeys(strikes)))

    cp = None
    if (" call" in ql) or ("calls" in ql) or ("call spread" in ql) or ("/c" in ql) or ql.endswith("c"):
        cp = "call"
    if (" put" in ql) or ("puts" in ql) or ("put spread" in ql) or ("/p" in ql) or ql.endswith("p"):
        cp = "put" if cp is None else cp

    expiry_hint = None
    if "0dte" in ql or "0-dte" in ql:
        expiry_hint = "0DTE"
    elif "today" in ql or "expiring today" in ql or "expires today" in ql:
        expiry_hint = "today"
    elif "this week" in ql:
        expiry_hint = "this_week"

    structure = None
    if "credit" in ql:
        structure = "credit_spread"
    elif "debit" in ql:
        structure = "debit_spread"
    elif "spread" in ql:
        structure = "spread"

    return {
        "raw": _truncate(q, 500),
        "strikes": strikes[:6],
        "cp": cp,
        "expiryHint": expiry_hint,
        "structureHint": structure,
        "notes": ["Parsed from user prompt (best-effort)."],
    }


def build_trade_brief(*, question: str, context_pack: Dict[str, Any]) -> Dict[str, Any]:
    """
    Derive desk-style, high-signal features (spot-to-strike distance, gamma context, regime/macro, technical proximity).
    """
    eng = str(context_pack.get("engine") or "").strip().lower()
    parsed = _parse_trade_from_prompt(question)

    # Best-effort spot
    spot = None
    if eng == "engine2":
        live = context_pack.get("liveContext") if isinstance(context_pack.get("liveContext"), dict) else {}
        dg = live.get("dealerGamma") if isinstance(live.get("dealerGamma"), dict) else {}
        if dg.get("spot") is not None:
            try:
                spot = float(dg.get("spot"))
            except Exception:
                spot = None
    tech = context_pack.get("technicals") if isinstance(context_pack.get("technicals"), dict) else {}
    if spot is None and isinstance(tech, dict) and tech.get("livePrice") is not None:
        try:
            spot = float(tech.get("livePrice"))
        except Exception:
            spot = None

    # Regime/macro
    cur = context_pack.get("current") if isinstance(context_pack.get("current"), dict) else {}
    reg = cur.get("regime") if isinstance(cur.get("regime"), dict) else {}
    macro = cur.get("macro") if isinstance(cur.get("macro"), dict) else {}

    # Dealer gamma
    live = context_pack.get("liveContext") if isinstance(context_pack.get("liveContext"), dict) else {}
    dg = live.get("dealerGamma") if isinstance(live.get("dealerGamma"), dict) else {}
    top_strikes = dg.get("topStrikes") if isinstance(dg.get("topStrikes"), list) else []

    # Strike distances
    dist = []
    if spot is not None and math.isfinite(float(spot)) and float(spot) > 0 and parsed.get("strikes"):
        s0 = float(spot)
        for k in parsed["strikes"]:
            try:
                kk = float(k)
            except Exception:
                continue
            pts = kk - s0
            pct = (pts / s0) * 100.0
            dist.append({"strike": int(kk), "pts": float(pts), "pct": float(pct)})

    # Technical proximity (EMA + Ichimoku cloud bounds + vwap proxy)
    prox = []
    if spot is not None and isinstance(tech, dict):
        ema = tech.get("ema") if isinstance(tech.get("ema"), dict) else {}
        for k in ("ema8", "ema21", "ema50", "ema100", "ema200"):
            if ema.get(k) is None:
                continue
            try:
                lv = float(ema.get(k))
                prox.append({"level": k, "price": lv, "pts": lv - float(spot), "pct": ((lv - float(spot)) / float(spot)) * 100.0})
            except Exception:
                continue
        ich = tech.get("ichimoku") if isinstance(tech.get("ichimoku"), dict) else {}
        cloud_now = ich.get("cloudNow") if isinstance(ich.get("cloudNow"), dict) else None
        if cloud_now:
            for k in ("cloudTop", "cloudBottom"):
                if cloud_now.get(k) is None:
                    continue
                try:
                    lv = float(cloud_now.get(k))
                    prox.append({"level": f"ichimoku.{k}", "price": lv, "pts": lv - float(spot), "pct": ((lv - float(spot)) / float(spot)) * 100.0})
                except Exception:
                    continue
        vp = tech.get("vwapProxy") if isinstance(tech.get("vwapProxy"), dict) else {}
        if vp and vp.get("vwap") is not None:
            try:
                lv = float(vp.get("vwap"))
                prox.append({"level": "vwapProxy", "price": lv, "pts": lv - float(spot), "pct": ((lv - float(spot)) / float(spot)) * 100.0})
            except Exception:
                pass

    return {
        "engine": eng,
        "parsedTrade": parsed,
        "spot": spot,
        "strikeDistances": dist,
        "regime": _pick(reg, ["score100", "bucket", "trend", "vol", "event", "stress", "dispersion"]),
        "macro": _pick(macro, ["multiplier", "tags", "highImpactUS"]),
        "dealerGamma": _pick(dg, ["netGammaSign", "magnitudeBucket", "spot", "band", "weighting", "expiry", "symbol"]),
        "dealerGammaTopStrikes": top_strikes[:10],
        "technicalProximity": prox[:16],
        "notes": ["TradeBrief is derived from RavenTech context + the user prompt (best-effort)."],
    }


def _fmt_pct(x: Any) -> str:
    try:
        if x is None:
            return "—"
        v = float(x)
        if not math.isfinite(v):
            return "—"
        return f"{v:.2f}%"
    except Exception:
        return "—"


def _fmt_num(x: Any, d: int = 2) -> str:
    try:
        if x is None:
            return "—"
        v = float(x)
        if not math.isfinite(v):
            return "—"
        return f"{v:.{int(d)}f}"
    except Exception:
        return "—"


def fallback_briefing(*, question: str, context_pack: Dict[str, Any]) -> str:
    """
    Deterministic, always-available response when the model returns empty/tool-only output.
    Uses Engine context + optional Benzinga news snapshot (if present).
    """
    eng = str(context_pack.get("engine") or "").strip() or "engine?"
    q = str(question or "").strip()

    lines: List[str] = []
    lines.append("Here’s a context-grounded briefing using RavenTech + any attached Benzinga headlines. (No web browsing beyond what’s provided.)")
    lines.append("")

    # --- Engine 2 (SPX) ---
    if eng == "engine2":
        cur = context_pack.get("current") if isinstance(context_pack.get("current"), dict) else {}
        reg = (cur.get("regime") if isinstance(cur.get("regime"), dict) else {}) if isinstance(cur, dict) else {}
        macro = (cur.get("macro") if isinstance(cur.get("macro"), dict) else {}) if isinstance(cur, dict) else {}
        live = context_pack.get("liveContext") if isinstance(context_pack.get("liveContext"), dict) else {}
        dg = live.get("dealerGamma") if isinstance(live.get("dealerGamma"), dict) else {}
        like = context_pack.get("oddsLikeNow") if isinstance(context_pack.get("oddsLikeNow"), dict) else {}

        score = reg.get("score100")
        bucket = reg.get("bucket")
        macro_mult = macro.get("multiplier")
        macro_hi = (macro.get("highImpactUS") if isinstance(macro.get("highImpactUS"), dict) else {}) if isinstance(macro, dict) else {}
        hi_top = macro_hi.get("top") if isinstance(macro_hi.get("top"), list) else []

        net_g = dg.get("netGammaSign")
        mag = dg.get("magnitudeBucket")
        spot = dg.get("spot")

        lines.append("## Snapshot")
        lines.append(f"- Regime: {_fmt_num(score, 1)} / 100 · {bucket or '—'}")
        lines.append(f"- Macro multiplier: {_fmt_num(macro_mult, 2)}×")
        if net_g or mag:
            lines.append(f"- Dealer gamma (live): {(str(net_g).upper() if net_g else '—')} · {(str(mag).upper() if mag else '—')} · spot={_fmt_num(spot, 2)}")
        lines.append("")

        lines.append("## Odds (like now)")
        byw = like.get("byWidth") if isinstance(like.get("byWidth"), list) else []
        if byw:
            for r in byw[:6]:
                if not isinstance(r, dict):
                    continue
                w = r.get("w")
                n = r.get("n")
                be = r.get("breachEitherPct")
                bp = r.get("breachPutPct")
                bc = r.get("breachCallPct")
                lines.append(f"- { _fmt_num(w,2) }× EM: breachEither={_fmt_pct(be)} (put={_fmt_pct(bp)} · call={_fmt_pct(bc)}) · n={n if n is not None else '—'}")
        else:
            lines.append("- No oddsLikeNow.byWidth available in context.")
        lines.append("")

        if hi_top:
            lines.append("## Scheduled macro that can gap / move the open (from your Benzinga macro calendar)")
            for x in hi_top[:8]:
                lines.append(f"- {str(x)}")
            lines.append("")

        # Strike / spread quick math (best-effort)
        try:
            # Prefer spot from dealer gamma, else technicals livePrice.
            px = None
            if spot is not None:
                px = float(spot)
            tech = context_pack.get("technicals") if isinstance(context_pack.get("technicals"), dict) else {}
            if px is None and tech and tech.get("livePrice") is not None:
                px = float(tech.get("livePrice"))
            if px is not None and math.isfinite(px):
                # parse strikes from question (very simple heuristic)
                import re

                # capture digits even when suffixed like "6955c" or "500p"
                nums = [int(n) for n in re.findall(r"(\d{4,5})(?:[cCpP])?", q)]
                nums = sorted(list(dict.fromkeys(nums)))
                # If user provided 6950/6955 style, these will show.
                if nums:
                    lines.append("## Your strikes (best-effort from your prompt)")
                    for s in nums[:4]:
                        diff = float(s) - float(px)
                        side = "above" if diff > 0 else "below"
                        lines.append(f"- Strike {s}: {abs(diff):.1f} pts {side} spot ({_fmt_num(px,2)})")
                    lines.append("")
        except Exception:
            pass

    # --- News snapshot if present ---
    news = context_pack.get("news") if isinstance(context_pack.get("news"), dict) else None
    if news and news.get("enabled") and isinstance(news.get("items"), list):
        items = [x for x in (news.get("items") or []) if isinstance(x, dict)]
        if items:
            lines.append("## Benzinga headlines snapshot (best-effort)")
            for it in items[:10]:
                title = str(it.get("title") or "").strip()
                if not title:
                    continue
                src = str(it.get("source") or "").strip()
                when = str(it.get("updated") or it.get("created") or "").strip()
                bit = title
                if src:
                    bit += f" ({src})"
                if when:
                    bit += f" · {when}"
                lines.append(f"- {bit}")
            lines.append("")

    # --- ORATS chain prefetch (if present) ---
    chain_pf = context_pack.get("oratsChainPrefetch") if isinstance(context_pack.get("oratsChainPrefetch"), dict) else None
    if chain_pf and chain_pf.get("ok") and isinstance(chain_pf.get("top"), dict):
        top = chain_pf.get("top") or {}
        lines.append("## ORATS chain slice (prefetch)")
        lines.append(f"- Expiry: {chain_pf.get('expiry') or '—'} · rowsUsed={chain_pf.get('rowsUsed')}")
        if isinstance(top.get("putOI"), list) and top.get("putOI"):
            p0 = top.get("putOI")[0]
            lines.append(f"- Put OI wall (top): strike={p0.get('strike')} · putOpenInterest={_fmt_num(p0.get('putOpenInterest'), 0)}")
        if isinstance(top.get("callOI"), list) and top.get("callOI"):
            c0 = top.get("callOI")[0]
            lines.append(f"- Call OI wall (top): strike={c0.get('strike')} · callOpenInterest={_fmt_num(c0.get('callOpenInterest'), 0)}")
        if isinstance(top.get("gamma"), list) and top.get("gamma"):
            g0 = top.get("gamma")[0]
            lines.append(f"- Gamma peak (top): strike={g0.get('strike')} · gamma={_fmt_num(g0.get('gamma'), 6)}")
        lines.append("")

    lines.append("## What can gap the open (framework)")
    lines.append("- Macro prints (CPI/FOMC/NFP minutes/claims, etc.), surprise policy headlines, geopolitical shocks, and big single-name earnings warnings are the classic gap drivers.")
    lines.append("- In holiday/low-liquidity tape, *smaller* catalysts can move price more than usual; treat gamma + liquidity as multipliers, not predictors.")
    lines.append("")
    lines.append("## What would change my view (quick questions)")
    lines.append("- What’s the **expiry** you’re trading (e.g., next Friday), and are you entering **Friday close** or **Monday open**?")
    lines.append("- What are your proposed **iron condor legs** (short put/call strikes + wing width) and your **target credit**?")
    lines.append("- What are your **management rules** (close at X% profit, max loss, roll triggers, delta/strike proximity triggers)?")

    return "\n".join(lines).strip()


def build_context_pack(*, engine: str, report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a compact, stable context pack for the LLM.
    We intentionally avoid dumping the entire report by default.
    """
    eng = str(engine)
    r = report or {}
    if not isinstance(r, dict):
        return {"engine": eng, "notes": ["Invalid report payload (not a dict)."]}

    if eng == "engine1":
        return {
            "engine": "engine1",
            "ticker": r.get("ticker"),
            "params": r.get("params"),
            "summary": r.get("summary"),
            "current": r.get("current"),
            "regime": r.get("regime"),
            "quarters": r.get("quarters"),
            "wingRecommendation": r.get("wingRecommendation"),
            "eventRisk": r.get("eventRisk"),
            "marketDealerGamma": r.get("marketDealerGamma"),
            "tickerDealerGamma": r.get("tickerDealerGamma"),
            "technicals": r.get("technicals"),
            "notes": [
                "Engine 1 context: earnings breach history + regime + overlays + technicals.",
                "When answering, cite numeric fields from this context; ask for missing trade specifics when needed (strikes/expiry/credit).",
            ],
        }

    if eng == "engine2":
        # Avoid huge grid payloads; keep only high-signal
        odds_like_now = r.get("oddsLikeNow") if isinstance(r.get("oddsLikeNow"), dict) else {}
        backtest = r.get("backtest") if isinstance(r.get("backtest"), dict) else {}
        return {
            "engine": "engine2",
            "asOfDate": r.get("asOfDate"),
            "params": r.get("params"),
            "underlying": r.get("underlying"),
            "current": r.get("current"),
            "liveContext": r.get("liveContext"),
            "oddsLikeNow": _pick(odds_like_now, ["regimeBucket", "macroBucket", "seasonBucket", "weeksUsed", "byWidth", "notes"]),
            "backtest": _pick(backtest, ["rowsUsed", "byWidth", "byQuarter", "notes"]),
            "technicals": r.get("technicals"),
            "notes": [
                "Engine 2 context: weekly expiry breach odds conditioned on regime/macro/season + live gamma context + technicals.",
                "Risk-only: no credit/PnL model unless provided by user.",
            ],
        }

    return {"engine": eng, "notes": ["Unknown engine."]}


ASKRAVEN_SYSTEM_PROMPT = """You are AskRaven, a senior quant trading assistant.\n\nHard rules:\n- Ground your answer in the provided RavenTech context pack. If a number is not in the context, say so.\n- If a question depends on missing trade inputs (credit, exact expiry time, structure, entry/stop), ask concise clarifying questions.\n- Distinguish between:\n  (1) historical odds from the engines,\n  (2) live/informational overlays (dealer gamma, live price), and\n  (3) outside context feeds explicitly provided in the context pack (Benzinga news, optional web results).\n- No hallucinated data. No fabricated citations.\n\nOutside context rules:\n- If Benzinga news is present in the context pack, summarize it and connect it to SPX drivers.\n- If web results are present in the context pack, cite sources with URLs.\n- If outside news is not present, say so, then proceed with context-based reasoning.\n\nOutput style:\n- Write like a trading-desk risk review: concise, skeptical, practical.\n- Use this structure (adapt as needed):\n  1) Position recap and context\n  2) Probabilistic framing (quant view)\n  3) Dealer gamma and strike gravity\n  4) Technical structure (daily + any uploaded charts)\n  5) Holiday/low-liquidity microstructure risks\n  6) Actionable decision tree (e.g., 2:30 / 3:00 / 3:30 ET)\n  7) Bottom line\n- Include a \"Key numbers\" block.\n- Include \"What would change my view\".\n\nCompliance:\n- Educational / risk analysis only; not financial advice.\n""".strip()


@dataclass(frozen=True)
class UploadedImage:
    content: bytes
    content_type: str
    image_id: str


def call_openai(
    *,
    question: str,
    context_pack: Dict[str, Any],
    images: List[UploadedImage],
) -> str:
    """
    Calls OpenAI Responses API. Pinned to GPT-5.2 by default.
    """
    api_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY on server.")

    # Lazy import so tests can monkeypatch without requiring network.
    from openai import OpenAI  # type: ignore

    client = OpenAI(api_key=api_key)

    model = str(os.getenv("OPENAI_MODEL") or "gpt-5.2").strip()
    max_out = int(float(os.getenv("ASKRAVEN_MAX_OUTPUT_TOKENS") or 900))
    effort = str(os.getenv("OPENAI_REASONING_EFFORT") or "auto").strip().lower()

    force_text = _env_bool("ASKRAVEN_FORCE_TEXT", True)
    enable_web = _env_bool("ASKRAVEN_ENABLE_WEB", False)
    wants_news = _wants_news_or_gap(question) or str(context_pack.get("engine") or "").strip().lower() == "engine2"

    enriched_ctx = dict(context_pack or {})
    # Add trade-derived features that make the answer look like a desk review.
    try:
        enriched_ctx["tradeBrief"] = build_trade_brief(question=question, context_pack=enriched_ctx)
    except Exception:
        enriched_ctx["tradeBrief"] = {"enabled": False, "notes": ["Failed to build tradeBrief."]}
    # Provide a small hint as to whether web tools are enabled.
    if enable_web and wants_news:
        enriched_ctx.setdefault("web", {"enabled": True, "provider": "openai_web_search", "notes": ["Web search tool enabled for news-type questions."]})

    ctx_txt = json.dumps(enriched_ctx, ensure_ascii=False, separators=(",", ":"), indent=2)
    base_user_text = f"RavenTech context pack:\n{ctx_txt}\n\nUser question:\n{str(question or '').strip()}"
    user_parts: List[Dict[str, Any]] = [{"type": "text", "text": base_user_text}]
    for img in images or []:
        try:
            url = encode_image_to_data_url(content=img.content, content_type=img.content_type)
            user_parts.append({"type": "input_image", "image_url": url})
        except Exception:
            continue

    base_system = ASKRAVEN_SYSTEM_PROMPT
    if force_text:
        base_system = base_system + "\n\nIMPORTANT: You must return a non-empty, plain-text answer. Do not return tool-only outputs."

    # Prefer Responses API if available (new SDK); otherwise fall back to Chat Completions.
    try:
        if hasattr(client, "responses") and getattr(client, "responses") is not None:
            def _responses_create(*, tools: Any | None, strict: bool) -> Any:
                sys_txt = base_system
                if strict:
                    sys_txt = sys_txt + "\n\nIf you are missing real-time news, explicitly say so, then proceed with a best-effort risk review using the provided context."
                kwargs: Dict[str, Any] = {
                    "model": model,
                    "input": [
                        {"role": "system", "content": [{"type": "text", "text": sys_txt}]},
                        {"role": "user", "content": user_parts},
                    ],
                    "max_output_tokens": max_out,
                }
                if tools is not None:
                    kwargs["tools"] = tools
                # Prefer reasoning control when available.
                try:
                    kwargs["reasoning"] = {"effort": effort}
                except Exception:
                    pass
                return client.responses.create(**kwargs)

            tools = None
            if enable_web and wants_news:
                # Try both tool names for compatibility across SDK versions.
                tools = [{"type": "web_search"}]

            try:
                resp = _responses_create(tools=tools, strict=False)
            except Exception:
                # Retry with alternate tool name if the SDK rejects this tool spec.
                if tools is not None:
                    try:
                        resp = _responses_create(tools=[{"type": "web_search_preview"}], strict=False)
                    except Exception:
                        resp = _responses_create(tools=None, strict=False)
                else:
                    resp = _responses_create(tools=None, strict=False)

            out = getattr(resp, "output_text", None)
            if isinstance(out, str) and out.strip():
                return out.strip()

            # If the model returned no text, retry once in strict mode, disabling tools.
            try:
                resp_retry = _responses_create(tools=None, strict=True)
                out2 = getattr(resp_retry, "output_text", None)
                if isinstance(out2, str) and out2.strip():
                    return out2.strip()
            except Exception:
                pass

            try:
                raw = resp.model_dump() if hasattr(resp, "model_dump") else {}
            except Exception:
                raw = {}
            # Last resort: if force_text, return a deterministic briefing; otherwise surface raw.
            return fallback_briefing(question=question, context_pack=enriched_ctx) if force_text else json.dumps(raw, ensure_ascii=False)[:4000]
    except AttributeError:
        # Older SDKs may not expose `responses`; fall through to chat.completions.
        pass

    # Chat Completions fallback (older SDK compatibility)
    chat_messages: List[Dict[str, Any]] = [
        {"role": "system", "content": base_system},
        {"role": "user", "content": [{"type": "text", "text": user_parts[0]["text"]}]},
    ]
    # Append images for vision-capable models (if supported).
    img_parts = []
    for img in images or []:
        try:
            url = encode_image_to_data_url(content=img.content, content_type=img.content_type)
            img_parts.append({"type": "image_url", "image_url": {"url": url}})
        except Exception:
            continue
    if img_parts:
        chat_messages[-1]["content"].extend(img_parts)

    def _chat_once(*, use_parts: bool) -> Any:
        if use_parts:
            msgs: List[Dict[str, Any]] = [
                {"role": "system", "content": base_system},
                {"role": "user", "content": [{"type": "text", "text": base_user_text}]},
            ]
            if img_parts:
                msgs[-1]["content"].extend(img_parts)
        else:
            # Plain string content is often the most compatible across models.
            msgs = [
                {"role": "system", "content": base_system},
                {"role": "user", "content": base_user_text},
            ]
        try:
            return client.chat.completions.create(model=model, messages=msgs, max_completion_tokens=max_out)
        except TypeError:
            return client.chat.completions.create(model=model, messages=msgs, max_tokens=max_out)

    try:
        # First attempt: rich parts (supports images).
        resp2 = _chat_once(use_parts=True)
    except Exception:
        # If the parts format is rejected by the model, retry with plain string messages.
        resp2 = _chat_once(use_parts=False)

    # Some models can return tool calls with empty text content. Never return blank.
    try:
        msg_obj = resp2.choices[0].message
        txt = _content_to_text(getattr(msg_obj, "content", None)).strip()
        if txt:
            return txt

        tool_calls = getattr(msg_obj, "tool_calls", None)
        fn_call = getattr(msg_obj, "function_call", None)
        if tool_calls or fn_call:
            # Try one more time with a very explicit plain-text constraint.
            try:
                strict_sys = base_system + "\n\nIMPORTANT: Return a non-empty plain-text answer. Do not use tools. Proceed with the provided context only."
                resp4 = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": strict_sys}, {"role": "user", "content": base_user_text}],
                    max_completion_tokens=max_out,
                )
                msg4 = resp4.choices[0].message
                txt4 = _content_to_text(getattr(msg4, "content", None)).strip()
                if txt4:
                    return txt4
            except Exception:
                pass
            return fallback_briefing(question=question, context_pack=enriched_ctx) if force_text else ""

        # Retry once with an explicit non-empty response constraint.
        try:
            resp3 = _chat_once(use_parts=False)
            msg3 = resp3.choices[0].message
            txt3 = _content_to_text(getattr(msg3, "content", None)).strip()
            if txt3:
                return txt3
        except Exception:
            pass

        return fallback_briefing(question=question, context_pack=enriched_ctx) if force_text else ""
    except Exception:
        try:
            raw2 = resp2.model_dump() if hasattr(resp2, "model_dump") else {}
        except Exception:
            raw2 = {}
        return json.dumps(raw2, ensure_ascii=False)[:4000]


