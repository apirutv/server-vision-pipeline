# services/vision_reasoner/main.py
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from common.logging import get_logger
from .reasoner import VisionReasonerConfig, build_plan
from .executor import RagClient, execute_plan
from .time_utils import fill_timestamps_from_iso

# --------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------

CONFIG_ENV_VAR = "SVP_CONFIG"
DEFAULT_CONFIG_PATH = "config/config.yaml"


def load_config() -> Dict[str, Any]:
    cfg_path = os.getenv(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH)
    path = Path(cfg_path)
    if not path.exists():
        raise RuntimeError(f"Config file not found at {path!s}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


config: Dict[str, Any] = load_config()

runtime_cfg: Dict[str, Any] = config.get("runtime", {})
frames_rag_cfg: Dict[str, Any] = config.get("frames_rag", {})
vr_cfg: Dict[str, Any] = config.get("vision_reasoner", {})

# --------------------------------------------------------------------
# Logging (common/logging.py)
# --------------------------------------------------------------------

LOG_DIR = runtime_cfg.get("log_dir", "logs")
LOG_LEVEL = runtime_cfg.get("log_level", "INFO")

# Ensure LOG_LEVEL env is set so other modules using get_logger() pick it up
os.environ.setdefault("LOG_LEVEL", LOG_LEVEL)

log = get_logger("vision_reasoner", log_dir=LOG_DIR, level=LOG_LEVEL)

log.info(
    "Loaded configuration sections",
    extra={
        "has_runtime": bool(runtime_cfg),
        "has_frames_rag": bool(frames_rag_cfg),
        "has_vision_reasoner": bool(vr_cfg),
    },
)

# --------------------------------------------------------------------
# Vision Reasoner config (from config.yaml -> vision_reasoner)
# --------------------------------------------------------------------

VISION_CFG = VisionReasonerConfig(vr_cfg)
TIMEZONE = VISION_CFG.timezone

# --------------------------------------------------------------------
# Registries: cameras / zones / event types
# --------------------------------------------------------------------

def _load_yaml(path_str: str) -> Dict[str, Any]:
    path = Path(path_str)
    if not path.exists():
        log.warning("YAML not found", extra={"path": path_str})
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        log.info("Loaded YAML", extra={"path": path_str})
        return data
    except Exception as e:
        log.exception("Failed to load YAML", extra={"path": path_str})
        raise RuntimeError(f"Failed to load YAML {path_str}: {e}") from e


def _build_registries_from_edge_config(edge_cfg_path: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Build CAMERAS_REGISTRY and ZONES_REGISTRY from edge-vision-pipeline config.yaml
    (copied as config/edge_config_read_only.yaml inside this repo).

    - cameras: list of {id, name, location, semantic_tags, observed_zones, ...}
    - zones:   list of {id, name, type, tags, ...}

    We convert:
      cameras -> {id: camera_dict}
      zones   -> {id: {zone_dict + camera_ids: [...]}}
    """
    raw = _load_yaml(edge_cfg_path)
    cameras_list = raw.get("cameras", []) or []
    zones_list = raw.get("zones", []) or []

    zones_by_id: Dict[str, Any] = {}
    for z in zones_list:
        zid = z.get("id")
        if not zid:
            continue
        zone = dict(z)
        zone.setdefault("camera_ids", [])
        zones_by_id[zid] = zone

    cameras_by_id: Dict[str, Any] = {}
    for c in cameras_list:
        cid = c.get("id")
        if not cid:
            continue
        cam = dict(c)
        # Normalize some fields to make life easier for the LLM:
        cam.setdefault("tags", cam.get("semantic_tags", []))
        cam.setdefault("zones", cam.get("observed_zones", []))
        cameras_by_id[cid] = cam

        for zone_id in cam.get("observed_zones", []) or []:
            if zone_id not in zones_by_id:
                # Zone referenced by camera but not defined in zones list:
                # create a minimal stub zone.
                zones_by_id[zone_id] = {
                    "id": zone_id,
                    "name": zone_id,
                    "type": "unknown",
                    "description": f"Auto-generated zone for {zone_id}",
                    "tags": [zone_id],
                    "camera_ids": [],
                }
            zones_by_id[zone_id].setdefault("camera_ids", [])
            if cid not in zones_by_id[zone_id]["camera_ids"]:
                zones_by_id[zone_id]["camera_ids"].append(cid)

    log.info(
        "Built registries from edge config",
        extra={
            "edge_cfg_path": edge_cfg_path,
            "num_cameras": len(cameras_by_id),
            "num_zones": len(zones_by_id),
        },
    )

    return cameras_by_id, zones_by_id


def _build_registries() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Prefer building from edge_config (config/edge_config_read_only.yaml).
    If that fails, fall back to separate cameras/zones YAMLs.
    """
    if VISION_CFG.edge_config:
        return _build_registries_from_edge_config(VISION_CFG.edge_config)

    # Fallback: legacy separate cameras/zones YAMLs
    cameras_raw = _load_yaml(VISION_CFG.cameras_config)
    zones_raw = _load_yaml(VISION_CFG.zones_config)

    cameras_by_id = cameras_raw.get("cameras", {})
    zones_by_id = zones_raw.get("zones", {})

    log.info(
        "Built registries from separate cameras/zones YAMLs",
        extra={
            "cameras_config": VISION_CFG.cameras_config,
            "zones_config": VISION_CFG.zones_config,
            "num_cameras": len(cameras_by_id),
            "num_zones": len(zones_by_id),
        },
    )

    return cameras_by_id, zones_by_id


CAMERAS_REGISTRY, ZONES_REGISTRY = _build_registries()

EVENT_TYPES: Dict[str, Any] = {
    "subjects": ["person", "vehicle", "pet"],
    "activities": [
        "entering",
        "leaving",
        "sitting",
        "group_presence",
        "moving",
        "presence",
    ],
}

if not CAMERAS_REGISTRY:
    log.warning("CAMERAS_REGISTRY is empty; camera inference will be limited")

# --------------------------------------------------------------------
# RAG client configuration (from frames_rag section)
# --------------------------------------------------------------------

frames_rag_host = frames_rag_cfg.get("host", "127.0.0.1")
frames_rag_port = frames_rag_cfg.get("port", 8080)
frames_rag_base_url = f"http://{frames_rag_host}:{frames_rag_port}"

RAG_CLIENT = RagClient(base_url=frames_rag_base_url)

log.info(
    "Vision Reasoner + RAG configuration initialized",
    extra={
        "ollama_url": VISION_CFG.ollama_base_url,
        "ollama_model": VISION_CFG.ollama_model,
        "clarify_threshold": VISION_CFG.clarify_threshold,
        "frames_rag_base_url": frames_rag_base_url,
        "timezone": TIMEZONE,
    },
)

# --------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------

app = FastAPI(title="NANA Vision Reasoner")


class ReasonRequest(BaseModel):
    query: str
    language: str | None = None  # optional hint: "en" or "th"


class ReasonResponse(BaseModel):
    plan: Dict[str, Any]
    search_query: str
    rag_result: Dict[str, Any]


@app.post("/reason/query", response_model=ReasonResponse)
async def reason_query(req: ReasonRequest):
    now_iso = datetime.now().isoformat()

    log.info("Received reasoning query", extra={"query": req.query})

    try:
        plan = await build_plan(
            cfg=VISION_CFG,
            cameras_registry=CAMERAS_REGISTRY,
            zones_registry=ZONES_REGISTRY,
            event_types=EVENT_TYPES,
            now_iso=now_iso,
            timezone=TIMEZONE,
            user_query=req.query,
        )
    except Exception as e:
        log.exception("Failed to build plan")
        raise HTTPException(status_code=500, detail=f"Plan error: {e}")

    # LLM decides from_iso/to_iso; we just parse to epoch seconds
    try:
        fill_timestamps_from_iso(plan, timezone_str=TIMEZONE)
    except Exception:
        log.exception("Failed to convert ISO times to timestamps")

    log.info(
        "Plan built",
        extra={
            "command_type": plan.command_type,
            "needs_clarification": plan.needs_clarification,
            "confidence": plan.confidence,
            "from_iso": plan.time_window.from_iso,
            "to_iso": plan.time_window.to_iso,
            "from_ts": plan.time_window.from_ts,
            "to_ts": plan.time_window.to_ts,
        },
    )

    if plan.needs_clarification:
        return ReasonResponse(
            plan=plan.model_dump(),
            search_query="",
            rag_result={
                "status": "needs_clarification",
                "clarification_question": plan.clarification_question,
            },
        )

    try:
        result = await execute_plan(
            rag=RAG_CLIENT,
            plan=plan,
        )
    except Exception as e:
        log.exception("Failed to execute plan")
        raise HTTPException(status_code=500, detail=f"Execution error: {e}")

    log.info("Plan executed successfully")

    return ReasonResponse(
        plan=result["plan"],
        search_query=result["search_query"],
        rag_result=result["rag_result"],
    )


@app.post("/reason/plan", response_model=ReasonResponse)
async def reason_plan_only(req: ReasonRequest):
    """
    Debug endpoint: build plan + parse time, but DO NOT call RAG.
    """
    now_iso = datetime.now().isoformat()

    log.info("Received plan-only request", extra={"query": req.query})

    try:
        plan = await build_plan(
            cfg=VISION_CFG,
            cameras_registry=CAMERAS_REGISTRY,
            zones_registry=ZONES_REGISTRY,
            event_types=EVENT_TYPES,
            now_iso=now_iso,
            timezone=TIMEZONE,
            user_query=req.query,
        )
        fill_timestamps_from_iso(plan, timezone_str=TIMEZONE)
    except Exception as e:
        log.exception("Failed to build plan (plan-only)")
        raise HTTPException(status_code=500, detail=f"Plan error: {e}")

    return ReasonResponse(
        plan=plan.model_dump(),
        search_query="",
        rag_result={"status": "plan_only"},
    )


def main():
    """
    Entrypoint for systemd template:
      svp@services.vision_reasoner.main
    """
    host = vr_cfg.get("host", "0.0.0.0")
    port = int(vr_cfg.get("port", 8011))

    log.info("Starting NANA Vision Reasoner", extra={"host": host, "port": port})

    uvicorn.run(
        "services.vision_reasoner.main:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
