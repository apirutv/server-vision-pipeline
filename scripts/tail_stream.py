# server-vision-pipeline/scripts/tail_stream.py
#!/usr/bin/env python3
from __future__ import annotations
import asyncio, argparse, json
from pathlib import Path
import yaml
from redis import asyncio as aioredis
from common.logging import get_logger

async def main():
    ap = argparse.ArgumentParser(description="Tail a Redis stream (read-only).")
    ap.add_argument("stream", help="Redis stream name, e.g. frames.described")
    ap.add_argument("--config", default="config/config.yaml", help="Path to config.yaml")
    ap.add_argument("--from-start", action="store_true", help="Read from oldest ('-') instead of latest ('$').")
    ap.add_argument("--batch", type=int, default=20, help="Number of messages per read (default: 20)")
    ap.add_argument("--log-dir", default="logs", help="Log directory for rotating logs")
    ap.add_argument("--log-level", default=None, help="Override log level (INFO/DEBUG/...)")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, "r"))
    rt = cfg.get("runtime", {}) or {}
    redis_url = rt.get("redis_url", "redis://127.0.0.1:6379/0")

    log = get_logger("tail_stream", log_dir=args.log_dir, level=(args.log_level or rt.get("log_level", "INFO")))
    log.info(f"Tailing stream='{args.stream}' from={'start' if args.from_start else 'latest'} @ {redis_url}")

    r = aioredis.from_url(redis_url, decode_responses=True)

    # '$' = only new messages; '-' = from beginning
    last_id = "-" if args.from_start else "$"

    try:
        while True:
            # XREAD returns list of (stream, [(id, {field: value}), ...])
            res = await r.xread({args.stream: last_id}, count=args.batch, block=5000)
            if not res:
                continue
            for _stream, messages in res:
                for msg_id, kv in messages:
                    last_id = msg_id
                    out = kv.get("json")
                    if out:
                        # pretty-print JSON (truncate if enormous)
                        try:
                            parsed = json.loads(out)
                            blob = json.dumps(parsed, ensure_ascii=False)
                        except Exception:
                            blob = out
                    else:
                        # raw key/value fallback
                        blob = json.dumps(kv, ensure_ascii=False)
                    if len(blob) > 1200:
                        blob = blob[:1200] + " …[truncated]"
                    log.info(f"{args.stream}@{msg_id} :: {blob}")
    except KeyboardInterrupt:
        log.info("Interrupted, exiting.")

if __name__ == "__main__":
    asyncio.run(main())
