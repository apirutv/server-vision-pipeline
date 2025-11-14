# server-vision-pipeline/services/ingest_api/main.py
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

import uvicorn
import yaml
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse

from common.logging import get_logger  # uses your rotating file+console logger

from redis import asyncio as aioredis

_redis = None
async def get_redis():
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


# -------------------------------------------------------------------
# Configuration & Logging
# -------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]
CFG_PATH = ROOT / "config" / "config.yaml"

if not CFG_PATH.exists():
    raise SystemExit(f"Config file not found: {CFG_PATH}")

cfg: Dict = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8")) or {}
rt = (cfg.get("runtime") or {})

HOST: str = rt.get("host", "0.0.0.0")
PORT: int = int(rt.get("port", 8000))
REDIS_URL: str = rt.get("redis_url", "redis://127.0.0.1:6379/0")  # used by scripts/tail_stream
INGEST_BASE: Path = Path(rt.get("ingest_base", str(ROOT / "data" / "landing")))
LOG_LEVEL: str = rt.get("log_level", "INFO")
LOG_DIR: str = rt.get("log_dir", "logs")

# ensure log level is picked up by our logger helper
os.environ["LOG_LEVEL"] = LOG_LEVEL
log = get_logger("ingest_api", log_dir=LOG_DIR, level=LOG_LEVEL)

# Ensure base dir exists
INGEST_BASE.mkdir(parents=True, exist_ok=True)

# -------------------------------------------------------------------
# FastAPI app
# -------------------------------------------------------------------
app = FastAPI(title="server-vision-pipeline :: Ingest API")


def _save_file(dst: Path, file: Optional[UploadFile]) -> int:
    """
    Save uploaded file to `dst`. Returns bytes written. If `file` is None,
    returns 0 and does nothing.
    """
    if not file:
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with dst.open("wb") as f:
        for chunk in iter(lambda: file.file.read(1024 * 1024), b""):
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
    return written


def _sha256_of(path: Path) -> str:
    """
    Compute SHA-256 of a file if it exists; return empty string otherwise.
    """
    if not path.exists() or not path.is_file():
        return ""
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


@app.post("/api/ingest/frame")
async def ingest_frame(
    manifest: str = Form(...),
    # optional files (client may omit some)
    frame: Optional[UploadFile] = File(None),
    tagged: Optional[UploadFile] = File(None),
    detections: Optional[UploadFile] = File(None),
    description: Optional[UploadFile] = File(None),
):
    """
    Receive a manifest (JSON) and up to four files:
      - frame:        original JPEG frame
      - tagged:       detector-annotated JPEG (boxes/labels)
      - detections:   detector JSON output
      - description:  LLM (vision) JSON output

    Writes to: {INGEST_BASE}/{YYYY}/{MM}/{DD}/{camera_id}/{frame_id}/
      manifest.json, frame.jpg, tagged.jpg, detections.json, description.json
    """
    try:
        man = json.loads(manifest)
    except Exception as e:
        log.error(f"Invalid manifest JSON: {e}")
        return JSONResponse({"error": "invalid manifest"}, status_code=400)

    frame_id = str(man.get("frame_id", "unknown")).strip() or "unknown"
    camera_id = str(man.get("camera_id", "unknown")).strip() or "unknown"
    ts_raw = str(man.get("ts") or "")

    # Normalize timestamp -> datetime
    try:
        dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).asthezone() if ts_raw else datetime.utcnow()
    except Exception:
        dt = datetime.utcnow()

    out_dir = INGEST_BASE / dt.strftime("%Y/%m/%d") / camera_id / frame_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Save files first so we can compute sizes/hashes for manifest enrichment
    bytes_written = {
        "frame": _save_file(out_dir / "frame.jpg", frame),
        "tagged": _save_file(out_dir / "tagged.jpg", tagged),
        "detections": _save_file(out_dir / "detections.json", detections),
        "description": _save_file(out_dir / "description.json", description),
    }

    # Compute hashes for saved files
    hashes = {
        "frame_sha256": _sha256_of(out_dir / "frame.jpg"),
        "tagged_sha256": _sha256_of(out_dir / "tagged.json"),  # note: file name is .jpg, fix below
        "detections_sha256": _sha256_of(out_dir / "detections.json"),
        "description_sha256": _sha256_of(out_dir / "desciption.json"),  # fix below
    }

    # FIX typos in file names
    hashes["tagged_sha256"] = _sha256_of(out_dir / "tagged.jpg")
    hashes["description_sha256"] = _sha256_of(out_dir / "description.json")

    # Merge a richer manifest for downstream indexing
    final_manifest = {
        **man,
        "saved_bytes": bytes_written,
        "hashes": hashes,
        "ingest": {
            "base": str(INGEST_BASE),
            "dir": str(out_dir),
            "received_at": datetime.utcnow().isoformat() + "Z",
        },
    }

    # Persist enriched manifest
    (out_dir / "manifest.json").write_text(
        json.dumps(final_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log.info(
        f"[ingest] camera={camera_id} frame={frame_id} "
        f"bytes={bytes_written} dir={out_dir}"
    )

    # Publish to server-side Redis for monitoring / indexing pipelines
    try:
        r = await get_redis()
        await r.xadd(
            "frames.ingested",
            {"json": json.dumps(final_manifest, ensure_ascii=False)},
            maxlen=50000,  # optional cap
            approximate=True
        )
        log.info(f"[redis] published frames.ingested frame={frame_id}")
    except Exception as e:
        log.warning(f"[redis] publish failed: {e}")


    return JSONResponse({"status": "ok", **final_manifest}, status_code=200)


# -------------------------------------------------------------------
# Entrypoint
# -------------------------------------------------------------------
if __name__ == "__main__":
    log.info(f"Starting ingest_api on {HOST}:{PORT} base={INGEST_BASE} log_dir={LOG_DIR}")
    uvicorn.run("services.ingest_api.main:app", host=HOST, port=PORT, reload=False, log_level=LOG_LEVEL.lower())
