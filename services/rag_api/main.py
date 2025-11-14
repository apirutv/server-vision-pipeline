from common.logging import get_logger
log = get_logger("rag_api")

@app.post("/rag/search")
def rag_search(req: SearchReq):
    log.info(f"/rag/search q='{req.q[:80]}...' cams={req.cameras} start={req.start} end={req.end} top_k={req.top_k}")
    ...
    log.info(f"/rag/search → {len(results)} result(s)")
    return {"results": results}

@app.post("/rag/get")
def rag_get(req: GetReq):
    log.info(f"/rag/get frame_id={req.frame_id}")
    ...

