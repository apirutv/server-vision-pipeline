from __future__ import annotations
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from common.logging import get_logger

log = get_logger("chroma_store")

class ChromaRAG:
    def __init__(self, path: str, collection: str, model_name: str):
        log.info(f"Init Chroma: path={path} collection={collection} model={model_name}")
        self.client = chromadb.PersistentClient(path=path)
        self.embed = SentenceTransformerEmbeddingFunction(model_name=model_name)
        self.col = self.client.get_or_create_collection(
            name=collection,
            embedding_function=self.embed,
            metadata={"hnsw:space": "cosine"},
        )
        log.info("Chroma collection ready")

    def upsert(self, ids, documents, metadatas):
        n = len(ids)
        self.col.upsert(ids=ids, documents=documents, metadatas=metadatas)
        log.info(f"Upserted {n} items")

    def query(self, q: str, n_results: int = 20, where: dict | None = None):
        log.debug(f"Query q='{q[:80]}...' n_results={n_results} where={where}")
        res = self.col.query(query_texts=[q], n_results=n_results, where=where or {})
        out = []
        for i, _id in enumerate(res["ids"][0]):
            out.append({
                "id": _id,
                "document": res["documents"][0][i],
                "metadata": res["metadatas"][0][i],
                "distance": res.get("distances", [[None]])[0][i],
            })
        log.debug(f"Query returned {len(out)} hits")
        return out

