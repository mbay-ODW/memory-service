"""CPU-only sentence-embedding service around intfloat/multilingual-e5-small.

e5 models are asymmetric: passages and queries must be embedded with different prefixes
("passage: " / "query: ") or retrieval quality degrades badly. That convention is baked in
here so no caller has to remember it. A model swap changes EMBEDDING_DIM and invalidates every
stored vector — see scripts/reembed_all.py.
"""

import asyncio
import threading

from app.config import get_settings

_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(get_settings().embedding_model_name)
    return _model


def embed_passage(text: str) -> list[float]:
    model = _get_model()
    return model.encode(f"passage: {text}", normalize_embeddings=True).tolist()


def embed_query(text: str) -> list[float]:
    model = _get_model()
    return model.encode(f"query: {text}", normalize_embeddings=True).tolist()


async def embed_passage_async(text: str) -> list[float]:
    return await asyncio.to_thread(embed_passage, text)


async def embed_query_async(text: str) -> list[float]:
    return await asyncio.to_thread(embed_query, text)
