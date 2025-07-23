from .embedder import Embedder
from .vector_store import VectorStore

class Retriever:
    def __init__(self, embedder: Embedder, store: VectorStore, top_k: int = 6):
        self.embedder = embedder
        self.store = store
        self.top_k = top_k

    def search(self, query: str, top_k: int = None):
        k = top_k or self.top_k
        vec = self.embedder.embed([query])[0]
        results = self.store.search(vec, k)
        # results: [((file, chunk_id, text), dist), ...]
        return [meta[2] for meta, _ in results]
