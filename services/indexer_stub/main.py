# server-vision-pipeline/services/indexer_stub/main.py
from __future__ import annotations
import argparse, json, os
from pathlib import Path
from datetime import datetime

def load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def flatten_record(root: Path, manifest: dict) -> dict:
    d = Path(manifest["ingest"]["dir"]) if "ingest" in manifest else root
    desc = load_json(d / "description.json") or {}
    dets = load_json(d / "detections.json") or {}

    # build a flat doc for indexing / NDJSON export
    return {
        "frame_id": manifest.get("frame_id"),
        "camera_id": manifest.get("camera_id"),
        "ts": manifest.get("ts"),
        "scene": manifest.get("scene"),
        "person_present": manifest.get("person_present"),
        "pet_present": manifest.get("pet_present"),
        "vehicles_present": manifest.get("vehicles_present"),
        "activities": manifest.get("activities", []),

        # file info
        "ingest_dir": str(d),
        "files": {
            "frame": str(d / "frame.jpg"),
            "tagged": str(d / "tagged.jpg"),
            "detections": str(d / "detections.json"),
            "description": str(d / "description.json"),
        },
        "hashes": manifest.get("hashes", {}),
        "saved_bytes": manifest.get("saved_bytes", {}),

        # embed-friendly text fields
        "objects": [o.get("label") for o in dets.get("objects", []) if isinstance(o, dict)],
        "people": [p.get("description") for p in (desc.get("people") or []) if isinstance(p, dict)],
        "pets": [p.get("description") for p in (desc.get("pets") or []) if isinstance(p, dict)],
        "vehicles": [v.get("description") for v in (desc.get("vehicles") or []) if isinstance(v, dict)],
        "scene_text": json.dumps({
            "scene": desc.get("scene"),
            "objects": desc.get("objects", []),
            "activities": desc.get("activities", []),
        }, ensure_ascii=False),
        "indexed_at": datetime.utcnow().isoformat() + "Z",
    }

def walk_manifests(landing_root: Path):
    for mp in landing_root.rglob("manifest.json"):
        yield mp

def main():
        ap = argparse.ArgumentParser(description="Build a flat NDJSON index from landing manifests.")
        ap.add_argument("--landing", default="data/landing", help="Root of landing tree")
        ap.add_argument("--out", default="data/index/frames.ndjson", help="Output NDJSON file")
        args = ap.parse_args()

        landing = Path(args.landing).resolve()
        out = Path(args.out).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)

        count = 0
        with out.open("w", encoding="utf-8") as fh:
            for mpath in walk_manifests(landing):
                man = load_json(mpath)
                if not man:
                    continue
                doc = flatten_record(landing, man)
                fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
                count += 1

        print(f"Wrote {count} records → {out}")

if __name__ == "__main__":
    main()
