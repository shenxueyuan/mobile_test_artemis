"""Bounded, text-only navigation decisions for the Flash runner."""

import asyncio
import math
import re
import time
from typing import Any

import httpx

from artemis.config.agent import JevFastConfig
from artemis.config.settings import settings
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

_ENDPOINTS = {
    "ofox": "https://api.ofox.ai/typesafe/v1/systemone",
    "typesafe": "https://api.typesafe.ai/v1/systemone",
}
_DANGEROUS = re.compile(
    r"删除|支付|购买|付款|提交|发送|确认|确定|允许|授权|卸载|清空|注销|"
    r"保存|安装|上传|发布|分享|拨打|拍照|录制|开启|关闭|同意|继续|下一步|完成|"
    r"delete|remove|pay|buy|purchase|submit|send|confirm|allow|grant|uninstall|reset|"
    r"sign out|save|install|upload|publish|share|call|record|enable|disable|toggle|"
    r"agree|continue|next|done|accept",
    re.IGNORECASE,
)
_VERBS = re.compile(
    r"^(?:打开|进入|查看|返回|前往|点开|点击|点按|选择|选中|"
    r"open|navigate|go to|view|click|tap|select)",
    re.IGNORECASE,
)
_CLAUSE_END = re.compile(r"[，。；、,.;\n]|然后|接着|随后|之后|以后|并且|后")


def _clauses(goal: str) -> list[str]:
    return [c.strip() for c in _CLAUSE_END.split(goal.strip()) if c.strip()]


def _eligible_clauses(goal: str) -> list[str]:
    """Safe step clauses: simple UI verbs and no dangerous wording."""
    return [c for c in _clauses(goal) if _VERBS.match(c) and not _DANGEROUS.search(c)]


def _clause_target(clause: str) -> str:
    return _VERBS.sub("", clause, count=1).strip(" :：。.【】「」()（）")


def _matches(label: str, clause: str, target: str) -> bool:
    label = label.strip().casefold()
    if not label:
        return False
    target = target.casefold()
    if target and (label == target or (len(target) >= 2 and target in label)):
        return True
    return len(label) >= 2 and label in clause.casefold()


def _objective(
    goal: str, candidates: list[dict[str, Any]], recent_actions: list[str]
) -> tuple[str | None, list[dict[str, Any]]]:
    """First safe clause still having on-screen matches and not yet tapped."""
    for clause in _eligible_clauses(goal):
        target = _clause_target(clause)
        hits = [c for c in candidates if _matches(str(c.get("text") or ""), clause, target)]
        if not hits:
            continue
        if any(_matches(a, clause, target) for a in recent_actions):
            continue
        return clause, hits
    return None, []


def jev_api_key(cfg: JevFastConfig):
    """Return only the credential belonging to the selected endpoint."""
    return settings.OFOX_API_KEY if cfg.provider == "ofox" else settings.TYPESAFE_API_KEY


def screen_signature(elements: list[dict[str, Any]]) -> tuple:
    """Detect a tap that left the actionable portion of the screen unchanged."""
    return tuple(
        (e.get("text"), e.get("resource_id"), tuple(e.get("bounds") or ()))
        for e in elements
        if e.get("clickable") and e.get("enabled") and not e.get("is_ocr")
    )


def navigation_candidates(
    goal: str, elements: list[dict[str, Any]], cfg: JevFastConfig
) -> list[dict[str, Any]]:
    if not _eligible_clauses(goal):
        return []
    candidates = []
    for position, element in enumerate(elements, start=1):
        label = str(element.get("text") or "").strip()
        bounds = element.get("bounds")
        if (
            not element.get("clickable")
            or not element.get("enabled")
            or element.get("is_ocr")
            or element.get("index") != position
            or not label
            or _DANGEROUS.search(label)
            or not isinstance(bounds, (tuple, list))
            or len(bounds) != 4
            or bounds[2] <= bounds[0]
            or bounds[3] <= bounds[1]
            or element.get("package") == "com.android.systemui"
        ):
            continue
        candidates.append(element)
    # Do not silently discard potentially relevant actions on crowded screens.
    if len(candidates) > cfg.max_candidates:
        return []
    return candidates


async def choose_navigation_action(
    goal: str,
    elements: list[dict[str, Any]],
    cfg: JevFastConfig,
    *,
    recent_actions: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[int | None, str, float]:
    """Return a current-screen index or defer to the existing multimodal model."""
    recent = recent_actions or []
    candidates = navigation_candidates(goal, elements, cfg)
    if not candidates:
        return None, "ineligible", 0.0
    clause, hits = _objective(goal, candidates, recent)
    if clause is None:
        return None, "no_target", 0.0
    # An element the instruction names unambiguously needs no model call.
    if len(hits) == 1:
        return int(hits[0]["index"]), "deterministic", 0.0
    labels = {str(e["index"]): e["text"] for e in hits}

    secret = jev_api_key(cfg)
    if not secret:
        return None, "no_key", 0.0
    payload = {
        "model": cfg.model,
        "state": {
            "goal": goal[:500],
            "current_objective": clause[:200],
            "screen": [
                {
                    "id": str(e["index"]),
                    "label": e["text"][:120],
                    "resource_id": e.get("resource_id"),
                }
                for e in hits
            ],
            "recent_actions": recent[-3:],
        },
        "questions": {
            "next_action": {
                "type": "choice",
                "instructions": (
                    "Choose one visible navigation control to advance the goal. "
                    "Choose DEFER if the goal is complete, unclear, or needs visual "
                    "reasoning, typing, permission, confirmation, or any side effect."
                ),
                "criteria": {**labels, "DEFER": "Let the full visual agent decide safely."},
            }
        },
    }
    started = time.perf_counter()
    try:
        async with asyncio.timeout(cfg.timeout_seconds):
            if client is None:
                async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as temporary_client:
                    response = await temporary_client.post(
                        _ENDPOINTS[cfg.provider],
                        json=payload,
                        headers={"Authorization": f"Bearer {secret.get_secret_value()}"},
                    )
            else:
                response = await client.post(
                    _ENDPOINTS[cfg.provider],
                    json=payload,
                    headers={"Authorization": f"Bearer {secret.get_secret_value()}"},
                )
        response.raise_for_status()
        answer = response.json()["answers"]["next_action"]
        selected = str(answer["choice"])
        probs = answer["probabilities"]
        if set(probs) != set(labels) | {"DEFER"}:
            return None, "invalid_response", time.perf_counter() - started
        if any(
            not math.isfinite(float(value)) or not 0 <= float(value) <= 1
            for value in probs.values()
        ):
            return None, "invalid_response", time.perf_counter() - started
        probability = float(probs[selected])
        margin = probability - max(
            (float(value) for key, value in probs.items() if key != selected),
            default=0.0,
        )
        if (
            selected not in labels
            or probability < cfg.confidence_threshold
            or margin < cfg.minimum_probability_margin
        ):
            return None, "deferred", time.perf_counter() - started
        return int(selected), "jev", time.perf_counter() - started
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        logger.warning(f"Jev navigation unavailable ({type(exc).__name__}); using Flash operator")
        return None, "unavailable", time.perf_counter() - started
