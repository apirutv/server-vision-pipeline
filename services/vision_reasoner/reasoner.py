# services/vision_reasoner/reasoner.py
from __future__ import annotations

import json
from typing import Any, Dict

import httpx
from pydantic import ValidationError

from common.logging import get_logger
from .schema import VisionQueryPlan
from .prompt import VISION_SYSTEM_PROMPT

# Reuse the same logger name as main.py
log = get_logger("vision_reasoner")

# Default location of the edge pipeline config, kept inside this repo
EDGE_CONFIG_DEFAULT = "config/edge_config_read_only.yaml"


class VisionReasonerConfig:
    """
    Strict config object loaded from config.yaml -> vision_reasoner section.
    All important values should come from the cfg dict, not hard-coded.

    Additionally, we assume a read-only copy of the edge-vision-pipeline
    config is located at config/edge_config_read_only.yaml inside this repo.
    """

    def __init__(self, cfg: Dict[str, Any]):
        if not isinstance(cfg, dict):
            raise ValueError("VisionReasonerConfig expects a dict from config.yaml")

        try:
            self.ollama_base_url: str = cfg["ollama_url"].rstrip("/")
            self.ollama_model: str = cfg["ollama_model"]
        except KeyError as e:
            raise RuntimeError(
                f"Missing required config value in vision_reasoner: {e}"
            )

        self.clarify_threshold: float = float(cfg.get("clarify_threshold", 0.6))
        self.timezone: str = cfg.get("timezone", "Asia/Bangkok")

        # Optional legacy paths for separate cameras/zones YAMLs
        self.cameras_config: str = cfg.get("cameras_config", "config/cameras.yaml")
        self.zones_config: str = cfg.get("zones_config", "config/zones.yaml")

        # Path to the read-only edge config inside this repo
        self.edge_config: str = cfg.get("edge_config", EDGE_CONFIG_DEFAULT)

        log.info(
            "VisionReasonerConfig initialized",
            extra={
                "ollama_base_url": self.ollama_base_url,
                "ollama_model": self.ollama_model,
                "clarify_threshold": self.clarify_threshold,
                "timezone": self.timezone,
                "cameras_config": self.cameras_config,
                "zones_config": self.zones_config,
                "edge_config": self.edge_config,
            },
        )


def build_developer_prompt(
    cameras_registry: Dict[str, Any],
    zones_registry: Dict[str, Any],
    event_types: Dict[str, Any],
    now_iso: str,
    timezone: str,
) -> str:
    """
    Injects runtime context into the developer message for the LLM.
    """
    payload = {
        "now": now_iso,
        "timezone": timezone,
        "cameras_registry": cameras_registry,
        "zones_registry": zones_registry,
        "event_types": event_types,
        "notes": (
            "You must only use camera_ids and zones from cameras_registry/zones_registry. "
            "If a user phrase can map to multiple cameras, prefer zone-level scope or "
            "ask for clarification."
        ),
    }

    log.debug(
        "Building developer prompt",
        extra={
            "num_cameras": len(cameras_registry or {}),
            "num_zones": len(zones_registry or {}),
            "timezone": timezone,
        },
    )

    return "RUNTIME CONTEXT:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


async def call_ollama_chat(
    cfg: VisionReasonerConfig,
    system_prompt: str,
    developer_prompt: str,
    user_query: str,
) -> Dict[str, Any]:
    """
    Calls Ollama's chat API and returns the parsed JSON from the response.
    """
    url = f"{cfg.ollama_base_url}/api/chat"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": developer_prompt},
        {"role": "user", "content": user_query},
    ]
    body = {
        "model": cfg.ollama_model,
        "messages": messages,
        "stream": False,
    }

    log.info(
        "Calling Ollama for reasoning plan",
        extra={"url": url, "model": cfg.ollama_model},
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        log.exception("Error calling Ollama chat API")
        raise

    content = data.get("message", {}).get("content", "").strip()
    log.debug(
        "Raw Ollama content received (truncated)",
        extra={"preview": content[:200]},
    )

    # The model has been instructed to output ONLY JSON
    try:
        parsed = json.loads(content)
        return parsed
    except json.JSONDecodeError:
        # In case the LLM still wraps JSON in text, try to extract it
        try:
            start = content.index("{")
            end = content.rindex("}") + 1
            parsed = json.loads(content[start:end])
            return parsed
        except Exception as e:
            log.exception("Failed to parse LLM plan JSON")
            raise RuntimeError(
                f"Failed to parse LLM plan JSON: {e}\nContent: {content}"
            ) from e


def _normalize_time_window_fields(plan_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    LLM sometimes puts ISO strings into from_ts/to_ts fields instead of from_iso/to_iso.
    This helper normalizes that BEFORE Pydantic validation:

    - If time_window.from_ts is a string and from_iso is missing, move it to from_iso.
    - If time_window.to_ts is a string and to_iso is missing, move it to to_iso.
    """
    tw = plan_dict.get("time_window")
    if not isinstance(tw, dict):
        return plan_dict

    from_ts = tw.get("from_ts")
    to_ts = tw.get("to_ts")

    # If from_ts is a string like "2025-11-12T00:00:00+07:00", treat it as from_iso
    if isinstance(from_ts, str):
        if not tw.get("from_iso"):
            tw["from_iso"] = from_ts
        # Clear from_ts so it can be set later by backend as float
        tw["from_ts"] = None

    # Same for to_ts
    if isinstance(to_ts, str):
        if not tw.get("to_iso"):
            tw["to_iso"] = to_ts
        tw["to_ts"] = None

    plan_dict["time_window"] = tw
    return plan_dict


async def build_plan(
    cfg: VisionReasonerConfig,
    cameras_registry: Dict[str, Any],
    zones_registry: Dict[str, Any],
    event_types: Dict[str, Any],
    now_iso: str,
    timezone: str,
    user_query: str,
) -> VisionQueryPlan:
    """
    Top-level orchestration: build developer prompt, call LLM, normalize output,
    validate into VisionQueryPlan.
    """
    log.info(
        "Building reasoning plan",
        extra={"query": user_query, "now": now_iso, "timezone": timezone},
    )

    developer_prompt = build_developer_prompt(
        cameras_registry=cameras_registry,
        zones_registry=zones_registry,
        event_types=event_types,
        now_iso=now_iso,
        timezone=timezone,
    )

    plan_dict = await call_ollama_chat(
        cfg=cfg,
        system_prompt=VISION_SYSTEM_PROMPT,
        developer_prompt=developer_prompt,
        user_query=user_query,
    )

    # --- NEW: normalize time_window fields before Pydantic validation ---
    plan_dict = _normalize_time_window_fields(plan_dict)

    try:
        plan = VisionQueryPlan.model_validate(plan_dict)
    except ValidationError as ve:
        log.exception("Plan validation error")
        raise RuntimeError(f"Plan validation error: {ve}\nPlan: {plan_dict}") from ve

    log.info(
        "Reasoning plan built successfully",
        extra={
            "command_type": plan.command_type,
            "needs_clarification": plan.needs_clarification,
            "confidence": plan.confidence,
        },
    )

    return plan
