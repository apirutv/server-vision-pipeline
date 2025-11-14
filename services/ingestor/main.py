from common.logging import get_logger
log = get_logger("ingestor")

def ingest_file(rag: ChromaRAG, json_path: Path):
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        frame_id = (data.get("_meta") or {}).get("frame_id") or data.get("frame_id")
        if not frame_id:
            log.warning(f"Skip (no frame_id): {json_path}")
            return False

        # ensure json_path recorded in metadata for later /rag/get
        meta = extract_meta(data)
        meta.setdefault("json_path", str(json_path.resolve()))

        doc = build_doc_string(data)
        rag.upsert(ids=[frame_id], documents=[doc], metadatas=[meta])
        log.info(f"Ingested frame_id={frame_id} cam={meta.get('camera_id')} ts={meta.get('ts')}")
        return True
    except Exception as e:
        log.error(f"Ingest error {json_path}: {e}")
        return False

def main():
    cfg = yaml.safe_load(open("config/config.yaml","r"))
    rag_cfg = cfg["rag"]; ing_cfg = cfg["ingest"]
    rag = ChromaRAG(rag_cfg["chroma_path"], rag_cfg["collection"], rag_cfg["embedding_model"])

    watch_dirs = ing_cfg.get("watch_dirs", [])
    pattern = ing_cfg.get("glob", "**/*.json")
    interval = int(ing_cfg.get("scan_interval_sec", 10))

    log.info(f"Watching {watch_dirs} pattern='{pattern}' interval={interval}s")
    seen = set()
    while True:
        new = 0
        for root in watch_dirs:
            for p in Path(root).glob(pattern):
                if p.suffix.lower() != ".json": continue
                if p in seen: continue
                if ingest_file(rag, p):
                    seen.add(p); new += 1
        if new:
            log.info(f"Ingested {new} new JSON(s) this cycle")
        time.sleep(interval)

