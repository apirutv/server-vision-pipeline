# server-vision-pipeline/services/indexer_worker/main.py
from __future__ import annotations

import asyncio, json, os
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

import yaml
from redis import asyncio as aioredis

from common.logging import get_logger  # uses your rotating file+console logger

# --------------------------- Config & logging ---------------------------

ROOT = Path(__file__).resolve().parents[2]
CFG_PATH = ROOT / "config" / "config.yaml"
cfg: Dict[str, Any] = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8")) if CFG_PATH.exists() else {}

rt          = (cfg.get("runtime") or {})
idx_cfg     = (cfg.get("indexer") or {})
idx_rt      = (idx_cfg.get("runtime") or {})

REDIS_URL   = idx_rt.get("redis_url", rt.get("redis_url", "redis://127.0.0.1:6379/0"))
STREAM_IN   = idx_rt.get("stream_in", "frames.ingested")
GROUP       = idx_rt.get("group", "indexer-worker")
CONSUMER    = idx_rt.get("consumer", "ix-01")
BATCH_SIZE  = int(idx_rt.get("batch_size", 32))
BLOCK_MS    = int(idx_rt.get("block_ms", 5000))
MIN_IDLE_MS = int(idx_rt.get("min_idle_ms", 5000))
DRAIN_HIST  = bool(idx_rt.get("drain_history", True))
DLQ_STREAM  = idx_rt.get("dlq_stream", "frames.indexer.dlq")

OUT_PATH    = Path(idx_cfg.get("out_path", "data/index/frames.ndjson"))
SEEN_PATH   = Path(idx_cfg.get("seen_path", "data/index/.seen_ids.txt"))
ENRICH_FROM_FILES = bool(idx_cfg.get("enrich_from_files", True))  # read description/detections off disk

LOG_LEVEL   = (rt.get("log_level", "INFO"))
LOG_DIR     = (rt.get("log_dir", "logs"))
os.environ["LOG_LEVEL"] = LOG_LEVEL
log = get_logger("indexer_worker", log_dir=LOG_DIR, level=LOG_LEVEL)

OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)

# --------------------------- Utilities ---------------------------

def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def _build_index_doc(man: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a flat, index-friendly record combining manifest + optional files
    (description.json, detections.json) found in the landing directory.
    """
    ingest = man.get("ingest", {}) or {}
    d = Path(ingest.get("dir", "")) if "dir" in ingest else None

    desc = {}
    dets = {}
    if ENRICH_FROM_FILES and d and d.exists():
        desc = _load_json(d / "description.json") or {}
        dets = _load_json(d / "detections.json") or {}

    # flatten a few helpful fields
    rec = {
        "frame_id": man.get("frame_id"),
        "camera_id": man.get("camera_id"),
        "ts": man.get("ts"),
        "scene": man.get("scene"),
        "person_present": man.get("person_present"),
        "pet_present": man.get("pet_present"),
        "vehicles_present": man.get("vehicles_present"),
        "activities": man.get("activities", []),
        "ingest_dir": str(d) if d else None,
        "files": {
            "frame": str(d / "frame.jpg") if d else None,
            "tagged": str(d / "tagged.jpg") if d else None,
            "detections": str(d / "detections.json") if d else None,
            "description": str(d / "description.json") if d else None,
        },
        "hashes": man.get("hashes", {}),
        "saved_bytes": man.get("saved_bytes", {}),
        "objects": [o.get("label") for o in (dets.get("objects") or []) if isinstance(o, dict)],
        "people": [p.get("description") for p in (desc.get("people") or []) if isinstance(p, dict)],
        "pets": [p.get("description") for p in (desc.get("pets") or []) if isinstance(p, dict)],
        "vehicles": [v.get("description") for v in (desc.get("vehicles") or []) if isinstance(v, dict)],
        "scene_text": json.dumps({
            "scene": (desc.get("scene")),
            "objects": (desc.get("objects") or []),
            "activities": (desc.get("activities") or []),
        }, ensure_ascii=False),
        "indexed_at": datetime.utcnow().isoformat() + "Z",
    }
    # drop Nones for cleanliness
    return {k: v for k, v in rec.items() if v is not None}

def _append_ndjson(doc: Dict[str, Any]) -> None:
    with OUT.PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, ensure_ascii=False) + "\n")

def _load_seen_ids() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    out = set()
    with SEEN_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if s:
                out.add(s)
    return out

def _append_seen_id(fid: str) -> None:
    with SEEN_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"{fid}\n")

def _normalize_payload(kv: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    frames.ingested is expected to be {"json": "<enriched manifest json>"}.
    """
    raw = kv.get("json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

# --------------------------- Redis group helpers ---------------------------

async def ensure_group(r):
    try:
        await r.xgroup_create(STREAM_IN, GROUP, id="0-0", mkstream=True)
        log.info(f"Created consumer group '{GROUP}' at 0-0 on '{STREAM_IN}'")
    except Exception as e:
        if "BUSYGROUP" in str(e):
            log.info(f"Consumer group '{GROUP}' already exists on '{STREAM_IN}'")
        else:
            raise

async def dlq(r, msg_id: str, kv: Dict[str, Any], error: str):
    try:
        await r.xadd(
            DLQ_STREAM,
            {"json": json.dumps({"source": STREAM_IN, "id": msg_id, "error": error, "kv": kv}, ensure_ascii=False)},
            maxlen=20000, approximate=True
        )
    except Exception as e:
        log.warning(f"[DLQ] failed to write DLQ: {e}")

# --------------------------- Phases ---------------------------

async def drain_history(r, seen: set[str]):
    if not DRAIN_HIST:
        log.info("Phase 1 skipped (drain_history=false)")
        return
    log.info("Phase 1: draining never-delivered history…")
    while True:
        resp = await r.xreadgroup(GROUP, CONSUMER, streams={STREAM_IN: "0"}, count=BATCH_SIZE)
        if not resp:
            break
        total = 0
        for _stream, messages in resp:
            total += len(messages)
            for msg_id, kv in messages:
                try:
                    man = _normalize_payload(kv)
                    if not man:
                        await dlq(r, msg_id, kv, "schema_mismatch")
                        await r.xack(STREAM_IN, GROUP, msg_id)
                        continue
                    fid = str(man.get("frame_id", "")).strip()
                    if fid and fid in seen:
                        log.debug(f"[skip] already indexed frame={fid}")
                        await r.xack(STREAM_IN, GROUP, msg_id)
                        continue
                    doc = _build_index_doc(man)
                    # append to NDJSON
                    with OUT_PATH.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
                    if fid:
                        _append_seen_id(fid)
                        seen.add(fid)
                    log.info(f"[indexed:H] frame={fid or 'unknown'}")
                    await r.xack(STREAM_IN, GROUP, msg_id)
                except Exception as e:
                    log.error(f"[history] error: {e}")
                    await dlq(r, msg_id, kv, f"exception:{e}")
                    await r.xack(STREAM_IN, GROUP, msg_id)
        if total == 0:
            break

async def recover_pending(r, seen: set[str]):
    log.info(f"Phase 2: recovering stale pending (min_idle_ms={MIN_IDLE_MS})…")
    cursor = "0-0"
    while True:
        try:
            next_cursor, claimed = await r.xautoclaim(
                STREAM_IN, GROUP, CONSUMER, MIN_IDLE_MS, start_id=cursor, count=BATCH_SIZE
            )
        except TypeError:
            try:
                next_cursor, claimed = await r.xautoclaim(
                    STREAM_IN, GROUP, CONSUMER, MIN_IDLE_MS, start=cursor, count=BATCH_SIZE
                )
            except Exception as e:
                log.warning(f"XAUTOCLAIM unsupported: {e} → skipping pending recovery")
                return
        except Exception as e:
            log.warning(f"XAUTOCLAIM failed: {e} → skipping pending recovery")
            return

        if not claimed:
            if next_cursor == cursor:
                return
            cursor = next_cursor
            continue

        for msg_id, kv in claimed:
            try:
                man = _normalize_payload(kv)
                if not man:
                    await dlq(r, msg_id, kv, "schema_mismatch")
                    await r.xack(STREAM_IN, GROUP, msg_id)
                    continue
                fid = str(man.get("frame_id", "")).strip()
                if fid and fid in seen:
                    log.debug(f"[skip] already indexed frame={fid}")
                    await r.xack(STREAM_IN, GROUP, msg_id)
                    continue
                doc = _build_index_doc(man)
                with OUT_PATH.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
                if fid:
                    _append_seen_id(fid)
                    seen.add(fid)
                log.info(f"[indexed:P] frame={fid or 'unknown'}")
                await r.xack(STREAM_IN, GROUP, msg_id)
            except Exception as e:
                log.error(f"[pending] error: {e}")
                await dlq(r, msg_id, kv, f"exception:{e}")
                await r.xack(STREAM_IN, GROUP, msg_id)

async def live_loop(r, seen: set[str]):
    log.info("Phase 3: live consumption (ID='>')…")
    while True:
        resp = await r.xreadgroup(GROUP, CONSUMER, streams={STREAM_IN: ">"}, count=BATCH_SIZE, block=BLOCK_MS)
        if not resp:
            continue
        for _stream, messages in resp:
            for msg_id, kv in messages:
                try:
                    man = _normalize_payload(kv)
                    if not man:
                        await dlq(r, msg_id, kv, "schema_mismatch")
                        await r.xack(STREAM_IN, GROUP, msg_id)
                        continue
                    fid = str(man.get("frame_id", "")).strip()
                    if fid and fid in seen:
                        log.debug(f"[skip] already indexed frame={fid}")
                        await r.xack(STREAM_IN, GROUP, msg_id)
                        continue
                    doc = _build_index_doc(man)
                    with OUT_PATH.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
                    if fid:
                        _append_seen_id(fid)
                        seen.add(fid)
                    log.info(f"[indexed:L] frame={fid or 'unknown'}")
                    await r.xack(STREAM_IN, GROUP, msg_id)
                except Exception as e:
                    log.error(f"[live] error: {e}")
                    await dlq(r, msg_id, kv, f"exception:{e}")
                    await r.xack(STREAM_IN, GROUP, msg_id)

# --------------------------- Main ---------------------------

async def main():
    log.info(f"indexer_worker starting… redis={REDIS_URL} stream={STREAM_IN} group={GROUP} out={OUT_PATH}")
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    await ensure_group(r)

    # Build in-memory set of already-indexed frame_ids for idempotency
    seen = _load_seen_ids()
    log.info(f"Loaded {len(seen)} previously indexed frame_ids")

    if DRAIN_HIST:
        await drain_history(r, seen)
    await recover_pending(r, seen)
    await live_loop(r, seen)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
