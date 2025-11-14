# server-vision-pipeline/scripts/verify_manifest.py
#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sys, hashlib
from pathlib import Path

def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            if not chunk: break
            h.update(chunk)
    return h.digest().hex()

def main():
    ap = argparse.ArgumentParser(description="Verify manifest hashes for a single landing directory.")
    ap.add_argument("dir", help="Path to landing dir (contains manifest.json, frame.jpg, etc.)")
    args = vars(ap.parse_args())
    d = Path(args["dir"]).resolve()
    mpath = d / "manifest.json"
    if not mpath.exists():
        print(f"manifest.json not found in {d}", file=sys.stderr); sys.exit(2)
    man = json.loads(mpath.read_text(encoding="utf-8"))
    expected = man.get("hashes", {})
    files = {
        "frame_sha": ("frame.jpg", expected.get("frame_sha256", "")),
        "tagged_sha": ("tagged.jpg", expected.get("tagged_sha256", "")),
        "detections_sha": ("detections.json", expected.get("detections_sha256", "")),
        "description_sha": ("description.json", expected.get("description_sha256", "")),
    }
    ok = True
    for label, (fname, exp) in files.items():
        p = d / fname
        if not exp:
            print(f"[WARN] No expected hash for {fname}")
            continue
        if not p.exists():
            print(f"[FAIL] Missing file: {fname} (expected {exp})"); ok = False; continue
        got = sha256(p)
        if got != exp:
            print(f"[FAIL] {fname}: expected {exp}, got {got}")
            ok = False
        else:
            print(f"[OK]   {fname}: {got}")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
