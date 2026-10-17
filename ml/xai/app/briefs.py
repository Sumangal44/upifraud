"""SOC brief generation: deterministic template + optional local vLLM LLM.

PII guard: briefs are built only from `tx_id`, VPA addresses, amounts, score,
action, reason codes and factor names. `note` and geo `city` are NEVER echoed;
`pii_guard()` redacts them defensively from any generated text (incl. LLM
output) if they ever appear.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from ml.common.schemas import ExplainRequest, FeatureVector, ScoreFactor

log = logging.getLogger(__name__)

#: Strict system prompt for the air-gapped SOC analyst agent.
SYSTEM_PROMPT = (
    "You are an air-gapped SOC analyst assistant running inside a UPI fraud "
    "monitoring platform. Produce a fraud brief of at most 120 words in plain "
    "English. Rules: cite ONLY the risk factors and facts provided in the "
    "user message — never invent entities, amounts or events; reference only "
    "VPA addresses and amounts as identity context; never mention transaction "
    "notes, cities, locations, IP addresses, device identifiers or any other "
    "PII; state the risk score, the action taken and why; output only the "
    "brief text with no preamble and no headings."
)

LLM_WORD_CAP = 120


def pii_guard(text: str, note: Optional[str] = None, city: Optional[str] = None) -> str:
    """Redact secrets that must never appear in a brief."""
    for secret in (note, city):
        if secret and len(str(secret).strip()) >= 2:
            text = text.replace(str(secret), "[redacted]")
    return text


def cap_words(text: str, limit: int = LLM_WORD_CAP) -> str:
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]) + "..."


def parties_from_features(features: FeatureVector) -> Optional[tuple[str, str, Optional[float]]]:
    """Derive `(src_vpa, dst_vpa, amount)` from the subgraph's latest transfer.

    `ExplainRequest` carries no `TransactionEvent`, so the most recent
    `TRANSFERRED` edge in the neighbourhood stands in for the transaction
    being explained (deterministic tie-break: timestamp, then src/dst id).
    """
    transferred = [
        e for e in features.subgraph.edges if e.rel == "TRANSFERRED" and e.amount is not None
    ]
    if not transferred:
        return None
    best = max(
        transferred,
        key=lambda e: (
            e.timestamp_ms if e.timestamp_ms is not None else -1,
            e.src,
            e.dst,
        ),
    )
    src = best.src.split(":", 1)[-1]
    dst = best.dst.split(":", 1)[-1]
    return src, dst, best.amount


def template_brief(req: ExplainRequest, attributions: list[ScoreFactor]) -> str:
    """Deterministic multi-sentence brief (no LLM, no PII)."""
    top = attributions[:3]
    factor_txt = ", ".join(f"{f.name} ({f.weight:+.3f})" for f in top) or "no dominant factors"
    score = req.decision.score
    action = req.decision.action.value
    past_tense = {"BLOCK": "BLOCKED", "CHALLENGE": "CHALLENGED", "ALLOW": "ALLOWED"}
    verb = past_tense.get(action, action)

    parties = parties_from_features(req.features)
    if parties is not None:
        src, dst, amount = parties
        head = f"Transaction {req.tx_id} from {src} to {dst} for ₹{amount:,.2f}"
    else:
        head = f"Transaction {req.tx_id}"

    if score >= 0.80:
        threshold_txt = (
            f"at or above the 0.80 block threshold, so settlement was blocked "
            f"and an account cooldown applies"
        )
    elif score >= 0.50:
        threshold_txt = (
            f"inside the 0.50-0.80 band, so a step-up challenge was issued"
        )
    else:
        threshold_txt = f"below the 0.50 challenge threshold, so it was authorized"

    reasons = [r for r in req.decision.reasons if r]
    reason_txt = f" Reason codes: {', '.join(reasons)}." if reasons else ""

    brief = (
        f"{head} scored {score:.3f} and was {verb} because {factor_txt}. "
        f"The blended score is {threshold_txt}.{reason_txt} "
        f"Model {req.decision.model_version}; review the cited factors with the "
        f"transaction record before any manual escalation."
    )
    return pii_guard(brief)


def _user_prompt(req: ExplainRequest, attributions: list[ScoreFactor]) -> str:
    parties = parties_from_features(req.features)
    if parties is not None:
        src, dst, amount = parties
        parties_txt = f" from {src} to {dst} for ₹{amount:,.2f}"
    else:
        parties_txt = ""
    factors = ", ".join(f"{f.name} ({f.weight:+.4f})" for f in attributions[:5])
    reasons = ", ".join(req.decision.reasons) or "none"
    return (
        f"Transaction {req.tx_id}{parties_txt}. "
        f"Score {req.decision.score:.4f}, action {req.decision.action.value}, "
        f"reason codes: {reasons}. "
        f"Top risk factors (name, signed weight): {factors}. "
        f"Write the fraud brief now."
    )


async def llm_brief(
    req: ExplainRequest,
    attributions: list[ScoreFactor],
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    note: Optional[str] = None,
    city: Optional[str] = None,
    timeout: float = 8.0,
) -> Optional[str]:
    """Call the local vLLM OpenAI-compatible endpoint; None on any failure."""
    import httpx  # deferred: only needed for the LLM path

    base_url = base_url if base_url is not None else os.environ.get("VLLM_BASE_URL", "")
    if not base_url:
        return None
    model = model if model is not None else os.environ.get(
        "VLLM_MODEL", "Qwen/Qwen2.5-7B-Instruct"
    )
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": 240,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(req, attributions)},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
    except Exception as exc:
        log.warning("vLLM brief failed, falling back to template: %s", exc)
        return None
    if not content or not str(content).strip():
        return None
    return cap_words(pii_guard(str(content).strip(), note, city))


def build_engine(attributions_from_model: bool, llm_ok: bool, model: str = "") -> str:
    """Engine label: `<attribution>+<brief>` per the wire contract examples."""
    if attributions_from_model:
        if not llm_ok:
            return "gnnexplainer+template"
        return "gnnexplainer+qwen2.5-7b" if "qwen" in model.lower() else "gnnexplainer+llm"
    return "heuristic+llm" if llm_ok else "heuristic+template"
