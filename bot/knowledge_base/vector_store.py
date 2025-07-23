import os, pickle
import numpy as np
import faiss

class VectorStore:
    def __init__(self, dim: int, path: str = "data/index.faiss"):
        self.dim = dim
        self.path = path
        self.index = faiss.IndexFlatL2(dim)
        self.meta = []
        if os.path.exists(path) and os.path.exists(path + ".meta"):
            self.load()

    def add(self, vectors: list[list[float]], meta_batch: list[tuple]):
        self.index.add(np.array(vectors, dtype="float32"))
        self.meta.extend(meta_batch)

    def search(self, vector: list[float], k: int = 5):
        if self.index.ntotal == 0:
            return []
        D, I = self.index.search(np.array([vector], dtype="float32"), k)
        results = []
        for pos, idx in enumerate(I[0]):
            if idx == -1: 
                continue
            results.append((self.meta[idx], float(D[0][pos])))
        return results

    def save(self):
        faiss.write_index(self.index, self.path)
        with open(self.path + ".meta", "wb") as f:
            pickle.dump(self.meta, f)

    def load(self):
        self.index = faiss.read_index(self.path)
        with open(self.path + ".meta", "rb") as f:
            self.meta = pickle.load(f)
