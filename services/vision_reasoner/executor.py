# services/vision_reasoner/executor.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from common.logging import get_logger
from .schema import VisionQueryPlan

# Reuse shared logger configured in main.py
log = get_logger("vision_reasoner")


class RagClient:
    def __init__(self, base_url: str = "http://localhost:8080"):
        """
        base_url SHOULD be passed from config.yaml via main.py.
        The default is only a safety fallback for tests.
        """
        self.base_url = base_url.rstrip("/")
        log.info("RagClient initialized", extra={"base_url": self.base_url})

    async def search(
        self,
        text_query: str,
        camera_ids: Optional[List[str]] = None,
        ts_from: Optional[float] = None,
        ts_to: Optional[float] = None,
        top_k: int = 100,
    ) -> Dict[str, Any]:
        """
        Adapts to your existing /rag/search API.
        Adjust the payload keys to match services/frames_rag/main.py.
        """
        params: Dict[str, Any] = {
            "q": text_query,
            "top_k": top_k,
        }
        if camera_ids:
            # encode as comma-separated if your /rag/search expects that
            params["cameras"] = ",".join(camera_ids)
        if ts_from is not None:
            params["ts_from"] = ts_from
        if ts_to is not None:
            params["ts_to"] = ts_to

        url = f"{self.base_url}/rag/search"

        log.info(
            "Calling /rag/search",
            extra={
                "url": url,
                "q": text_query,
                "cameras": camera_ids,
                "ts_from": ts_from,
                "ts_to": ts_to,
                "top_k": top_k,
            },
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            log.exception("Error calling /rag/search")
            raise

        hits = data.get("results") or data.get("items") or []
        log.info("RAG search completed", extra={"num_results": len(hits)})

        return data


def build_text_query_from_plan(plan: VisionQueryPlan) -> str:
    """
    Use the semantic info from the plan to construct a free-text query string
    for the RAG service. This is intentionally simple: you can upgrade later.
    """
    parts: List[str] = []

    # Subjects and activities become positive terms
    if plan.event_filter.subjects:
        parts.extend(plan.event_filter.subjects)
    if plan.event_filter.activities:
        parts.extend(plan.event_filter.activities)

    # Optionally, zone names as soft hints
    if plan.target_scope.zones:
        parts.extend(plan.target_scope.zones)

    # For indirect / pattern questions, add generic nouns
    if plan.command_type == "indirect":
        parts.append("person")

    if not parts:
        query = "person"  # safe default for presence/security
    else:
        # dedupe while preserving order
        query = " ".join(dict.fromkeys(parts))

    log.debug(
        "Built text query from plan",
        extra={
            "command_type": plan.command_type,
            "subjects": plan.event_filter.subjects,
            "activities": plan.event_filter.activities,
            "zones": plan.target_scope.zones,
            "query": query,
        },
    )

    return query


async def execute_plan(
    rag: RagClient,
    plan: VisionQueryPlan,
) -> Dict[str, Any]:
    """
    Executes the plan via /rag/search (and later /rag/aggregate) and returns
    a structure that the FastAPI endpoint can post-process for the client.
    """
    log.info(
        "Executing plan against RAG",
        extra={
            "command_type": plan.command_type,
            "aggregation_mode": plan.aggregation.mode,
            "scope_type": plan.target_scope.scope_type,
        },
    )

    text_query = build_text_query_from_plan(plan)

    ts_from = plan.time_window.from_ts
    ts_to = plan.time_window.to_ts

    # Camera scope: if scope_type is zone or whole_house, we assume a
    # higher layer has already resolved zones->cameras in the plan.
    cameras = plan.target_scope.cameras or None

    search_result = await rag.search(
        text_query=text_query,
        camera_ids=cameras,
        ts_from=ts_from,
        ts_to=ts_to,
        top_k=plan.aggregation.top_k,
    )

    log.info(
        "Plan execution completed",
        extra={
            "query": text_query,
            "num_cameras": len(cameras or []),
            "top_k": plan.aggregation.top_k,
        },
    )

    return {
        "plan": plan.model_dump(),
        "search_query": text_query,
        "rag_result": search_result,
    }
