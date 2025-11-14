# 🧠 Server Vision Pipeline (Ubuntu / Cloud)

> **Purpose**
>
> The *server-vision-pipeline* is the central reasoning and retrieval hub for all edge cameras.  
> It receives scene JSONs from Pis, stores all artifacts, maintains a FAISS + SQLite RAG index, and exposes `/rag/*` + `/chat` APIs for semantic search and LLM reasoning.

---

## 🌐 1. Architecture

```text
edge-vision-pipeline (Pi) → uploader_ingest → ingest_api (server)
                                 │
                                 ▼
                      data/landing/YYYY/MM/DD/<camera>/<frame_id>/
                                 │
                                 ▼
                        Redis stream → frames.ingested
                                 │
                                 ▼
                       indexer_worker → data/index/frames.ndjson
                                 │
                                 ▼
                  frames_rag (FAISS + SQLite + Ollama embeddings)
                                 │
                                 ▼
                     nana_reasoner (LLM reasoning via Ollama)
```

---

## 🧱 2. Core Services

| Service | Function |
|----------|-----------|
| `ingest_api` | Accepts uploads (frame, tagged, detections, description + manifest) and writes bundles under `data/landing/`. Publishes `frames.ingested` to Redis. |
| `indexer_worker` | Consumes `frames.ingested`, merges the bundle, and appends a flattened record to `data/index/frames.ndjson`. |
| `frames_rag` | Builds and serves a FAISS + SQLite RAG index over `frames.ndjson` using Ollama embeddings (`nomic-embed-text`). Exposes `/rag/rebuild`, `/rag/refresh`, `/rag/search`. |
| `nana_reasoner` | NANA Camera Reasoning Agent — interprets natural queries, infers cameras/time, calls `/rag/search`, and summarizes results. |
| `camera_resolver` | `/cameras/resolve?q=` based on `config/cameras.yaml` (maps phrases → camera IDs). |
| `redis_dashboard` | Dark-mode web dashboard for Redis lag/pending + live logs. |

---

## ⚙️ 3. Configuration (`config/config.yaml`)

```yaml
runtime:
  redis_url: "redis://127.0.0.1:6379/0"
  ingest_base: "data/landing"
  host: "0.0.0.0"
  port: 8000
  log_level: "INFO"
  log_dir: "logs"

frames_rag:
  ndjson_path: "data/index/frames.ndjson"
  db_path: "data/rag/frames.sqlite"
  faiss_path: "data/rag/faiss.index"
  ids_path: "data/rag/ids.json"
  state_path: "data/rag/state.json"
  ollama_url: "http://127.0.0.1:11434"
  embed_model: "nomic-embed-text"
  host: "0.0.0.0"
  port: 8080

indexer:
  runtime:
    redis_url: "redis://127.0.0.1:6379/0"
    stream_in: "frames.ingested"
    group: "indexer-worker"
    consumer: "ix-01"

redis_dashboard:
  host: "0.0.0.0"
  port: 9091
  refresh_ms: 2000
```

---

## 📡 4. Redis Streams (Server Side)

| Stream | Producer | Consumer | Description |
|---------|-----------|-----------|-------------|
| `frames.ingested` | `ingest_api` | `indexer_worker` | Ingested bundle manifest per frame |
| `frames.indexer.dlq` | `indexer_worker` | (debug) | Failed or malformed ingests (if you add DLQ) |

---

## 🧪 5. Run Manually (for development)

```bash
cd ~/python_projects/server-vision-pipeline
source .venv/bin/activate

# make sure redis-server is running
sudo systemctl start redis-server

python -m services.ingest_api.main
python -m services.indexer_worker.main
python -m services.frames_rag.main
python -m services.nana_reasoner.main
python -m services.camera_resolver.main
python -m services.redis_dashboard.main
```

---

## ⚙️ 6. Run as **systemd** Services (Server)

Template `/etc/systemd/system/svp@.service`:

```ini
[Unit]
Description=Server Vision Pipeline - %i
After=network-online.target redis-server.service
Wants=network-online.target

[Service]
User=apirut
WorkingDirectory=/home/apirut/python_projects/server-vision-pipeline
Environment=PYTHONUNBUFFERED=1
Environment=LOG_LEVEL=INFO
ExecStart=/home/apirut/python_projects/server-vision-pipeline/.venv/bin/python -m %i
Restart=always
RestartSec=2
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Enable services:

```bash
sudo systemctl daemon-reload

sudo systemctl enable --now svp@services.ingest_api.main
sudo systemctl enable --now svp@services.indexer_worker.main
sudo systemctl enable --now svp@services.frames_rag.main
sudo systemctl enable --now svp@services.nana_reasoner.main
sudo systemctl enable --now svp@services.camera_resolver.main
sudo systemctl enable --now svp@services.redis_dashboard.main
```

View logs:

```bash
journalctl -u svp@services.frames_rag.main -f
```

---

## ⏱️ 7. RAG Refresh & Rebuild Timers

### Refresh every 5 minutes

`/etc/systemd/system/svp-rag-refresh.service`:

```ini
[Unit]
Description=Server Vision Pipeline - RAG refresh (tail-only)

[Service]
Type=oneshot
User=apirut
ExecStart=/usr/bin/curl -sS http://127.0.0.1:8080/rag/refresh
```

`/etc/systemd/system/svp-rag-refresh.timer`:

```ini
[Unit]
Description=Run RAG refresh every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true
Unit=svp-rag-refresh.service

[Install]
WantedBy=timers.target
```

### Weekly full rebuild (Sunday 03:00)

`/etc/systemd/system/svp-rag-rebuild.service`:

```ini
[Unit]
Description=Server Vision Pipeline - RAG full rebuild
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=apirut
ExecStart=/usr/bin/curl -sS http://127.0.0.1:8080/rag/rebuild
```

`/etc/systemd/system/svp-rag-rebuild.timer`:

```ini
[Unit]
Description=Run RAG full rebuild weekly (Sun 03:00)

[Timer]
OnCalendar=Sun *-*-* 03:00:00
Persistent=true
Unit=svp-rag-rebuild.service

[Install]
WantedBy=timers.target
```

Enable timers:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now svp-rag-refresh.timer
sudo systemctl enable --now svp-rag-rebuild.timer
systemctl list-timers | grep svp-rag
```

---

## 📊 8. Server Dashboard

```text
http://<SERVER_IP>:9091/
```

- Dark‑mode **Server Vision Pipeline Redis Dashboard**  
- Shows Redis lag/pending + live logs for server services (ingest_api, indexer_worker, frames_rag, nana_reasoner, camera_resolver).

---

## 🔍 9. Using `tail_stream.py` on the Server

`scripts/tail_stream.py` lets you inspect Redis streams directly.

Examples (server-side Redis):

```bash
# Inspect ingestion stream
python -m scripts.tail_stream frames.ingested

# Inspect indexer DLQ (if configured)
python -m scripts.tail_stream frames.indexer.dlq
```

Tail Pi streams from the server (remote Redis on Pi):

```bash
# Tail frames.described from the Pi5 (replace IP)
python -m scripts.tail_stream frames.described --url redis://192.168.0.169:6379/0
```

You can use this tool from either side to debug end‑to‑end flow.

---

## ✅ 10. Summary

- All server services are managed via systemd (`svp@…`) and auto‑restart on failure.  
- RAG index stays up to date via `/rag/refresh` timer and `/rag/rebuild` weekly maintenance.  
- Redis dashboard and `tail_stream.py` provide real‑time visibility into streams and logs.  
- The server forms the “brain” of the Edge Vision system: ingesting, indexing, and reasoning over every frame description.
