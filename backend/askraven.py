from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from backend.technicals import encode_image_to_data_url


def _pick(d: dict, keys: List[str]) -> dict:
    out = {}
    for k in keys:
        if k in d:
            out[k] = d.get(k)
    return out


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


ASKRAVEN_SYSTEM_PROMPT = """You are AskRaven, a rigorous, skeptical quant trading assistant.\n\nHard rules:\n- Ground your answer in the provided RavenTech context pack. If a number is not in the context, say so.\n- If a question depends on missing trade inputs (credit, exact expiry, wing width, underlying proxy), ask concise clarifying questions.\n- Distinguish between:\n  (1) historical odds from the engines,\n  (2) live/informational overlays (dealer gamma, live price), and\n  (3) outside context feeds explicitly provided in the context pack (e.g., Benzinga news).\n- No hallucinated data. No fabricated citations.\n\nConstraints:\n- You do NOT have web browsing unless explicitly provided.\n- If the user asks for “news of the day” or headlines:\n  - Use the news items provided in the context pack (if present) and label them as Benzinga news.\n  - If news items are not present, say so and proceed with context-based reasoning.\n\nOutput style:\n- Prefer bullet points.\n- Include a short \"Key numbers\" section when possible.\n- Include a short \"What would change my view\" section.\n\nCompliance:\n- Educational / risk analysis only; not financial advice.\n""".strip()


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

    ctx_txt = json.dumps(context_pack, ensure_ascii=False, separators=(",", ":"), indent=2)
    base_user_text = f"RavenTech context pack:\n{ctx_txt}\n\nUser question:\n{str(question or '').strip()}"
    user_parts: List[Dict[str, Any]] = [{"type": "text", "text": base_user_text}]
    for img in images or []:
        try:
            url = encode_image_to_data_url(content=img.content, content_type=img.content_type)
            user_parts.append({"type": "input_image", "image_url": url})
        except Exception:
            continue

    # Prefer Responses API if available (new SDK); otherwise fall back to Chat Completions.
    try:
        if hasattr(client, "responses") and getattr(client, "responses") is not None:
            # Prefer reasoning control when available; fall back if the SDK/model rejects it.
            try:
                resp = client.responses.create(
                    model=model,
                    input=[
                        {"role": "system", "content": [{"type": "text", "text": ASKRAVEN_SYSTEM_PROMPT}]},
                        {"role": "user", "content": user_parts},
                    ],
                    max_output_tokens=max_out,
                    reasoning={"effort": effort},
                )
            except Exception:
                resp = client.responses.create(
                    model=model,
                    input=[
                        {"role": "system", "content": [{"type": "text", "text": ASKRAVEN_SYSTEM_PROMPT}]},
                        {"role": "user", "content": user_parts},
                    ],
                    max_output_tokens=max_out,
                )

            out = getattr(resp, "output_text", None)
            if isinstance(out, str) and out.strip():
                return out.strip()

            # If the model returned no text, retry once with a stricter instruction to always respond.
            try:
                resp_retry = client.responses.create(
                    model=model,
                    input=[
                        {
                            "role": "system",
                            "content": [
                                {
                                    "type": "text",
                                    "text": ASKRAVEN_SYSTEM_PROMPT
                                    + "\n\nIMPORTANT: You must return a non-empty text answer. If real-time news is unavailable, say so and proceed with context-based reasoning.",
                                }
                            ],
                        },
                        {"role": "user", "content": [{"type": "text", "text": base_user_text}]},
                    ],
                    max_output_tokens=max_out,
                )
                out2 = getattr(resp_retry, "output_text", None)
                if isinstance(out2, str) and out2.strip():
                    return out2.strip()
            except Exception:
                pass

            try:
                raw = resp.model_dump() if hasattr(resp, "model_dump") else {}
            except Exception:
                raw = {}
            return json.dumps(raw, ensure_ascii=False)[:4000]
    except AttributeError:
        # Older SDKs may not expose `responses`; fall through to chat.completions.
        pass

    # Chat Completions fallback (older SDK compatibility)
    chat_messages: List[Dict[str, Any]] = [
        {"role": "system", "content": ASKRAVEN_SYSTEM_PROMPT},
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
                {"role": "system", "content": ASKRAVEN_SYSTEM_PROMPT},
                {"role": "user", "content": [{"type": "text", "text": base_user_text}]},
            ]
            if img_parts:
                msgs[-1]["content"].extend(img_parts)
        else:
            # Plain string content is often the most compatible across models.
            msgs = [
                {"role": "system", "content": ASKRAVEN_SYSTEM_PROMPT},
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
        txt = getattr(msg_obj, "content", None)
        if isinstance(txt, str) and txt.strip():
            return txt.strip()

        tool_calls = getattr(msg_obj, "tool_calls", None)
        fn_call = getattr(msg_obj, "function_call", None)
        if tool_calls or fn_call:
            return (
                "I can’t fetch real-time headlines yet because this AskRaven instance isn’t configured with a news/browsing tool. "
                "What I *can* do right now is: use your Engine 2 macro calendar + regime + dealer gamma context, and interpret any charts you upload. "
                "If you want true “news of the day,” we can wire a news feed (e.g., Benzinga News endpoint) and I’ll summarize it pre-market."
            )

        # Retry once with an explicit non-empty response constraint.
        try:
            resp3 = _chat_once(use_parts=False)
            msg3 = resp3.choices[0].message
            txt3 = getattr(msg3, "content", None)
            if isinstance(txt3, str) and txt3.strip():
                return txt3.strip()
        except Exception:
            pass

        return (
            "I didn’t receive any text back from the model for that request. "
            "This can happen when the model tries to use a tool (news/browsing) that isn’t configured. "
            "If you want real-time news/headlines, we need to add a news feed tool (Benzinga News or web search)."
        )
    except Exception:
        try:
            raw2 = resp2.model_dump() if hasattr(resp2, "model_dump") else {}
        except Exception:
            raw2 = {}
        return json.dumps(raw2, ensure_ascii=False)[:4000]


