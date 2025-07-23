import os, json, time
from typing import Dict, Optional
from .yandex_client import YandexDiskClient
from .loaders import EXT_LOADERS, PasswordRequired
from .splitter import split_text
from .embedder import Embedder
from .vector_store import VectorStore

INDEX_STATE = "data/kb_state.json"

def load_state() -> Dict[str, str]:
    if os.path.exists(INDEX_STATE):
        with open(INDEX_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state: Dict[str, str]):
    os.makedirs(os.path.dirname(INDEX_STATE), exist_ok=True)
    with open(INDEX_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

async def reindex(root_path: str, yd: YandexDiskClient, store: VectorStore, emb: Embedder, pdf_passwords: Dict[str, str], chunk_tokens=500, overlap=50, model="gpt-4o-mini", progress_cb=None):
    """Асинхронная индексация. progress_cb(step, total, filename) -> None"""
    state = load_state()
    files = list(yd.iter_files(root_path))
    total = len(files)
    added = 0
    for idx, (remote_path, _) in enumerate(files, start=1):
        if progress_cb:
            progress_cb(idx, total, remote_path)
        try:
            content = yd.download(remote_path)
        except Exception as e:
            continue
        sig = YandexDiskClient.file_signature(content)
        if state.get(remote_path) == sig:
            continue  # unchanged
        ext = os.path.splitext(remote_path)[1].lower()
        loader = EXT_LOADERS.get(ext)
        if not loader:
            continue
        text = ""
        try:
            if ext==".pdf":
                password = pdf_passwords.get(os.path.basename(remote_path))
                text = loader(content, password=password)
            else:
                text = loader(content)
        except PasswordRequired:
            # пропускаем, попросим пароль у пользователя
            continue
        chunks = split_text(text, max_tokens=chunk_tokens, overlap=overlap, model=model)
        vectors = emb.embed(chunks)
        meta = [(remote_path, i, chunks[i]) for i in range(len(chunks))]
        store.add(vectors, meta)
        state[remote_path] = sig
        added += 1
    store.save()
    save_state(state)
    return added, total
