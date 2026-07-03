"""CAG-кэш (Cache-Augmented Generation) — заимствование идеи у janson.

Две части:
  1. chunk_point_id(text) — детерминированный ID точки Qdrant по SHA-256
     нормализованного текста. Одинаковые чанки (boilerplate журналов)
     схлопываются в одну точку → нет дублей и повторного эмбеддинга.
  2. AnswerCache — потокобезопасный кэш ответов Q&A с TTL и LRU-вытеснением.
     Ключ = хэш(нормализованный вопрос | geo | intent_hint).
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from collections import OrderedDict

from app.config import settings


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def chunk_content_hash(text: str) -> str:
    return hashlib.sha256(_norm_text(text).encode("utf-8")).hexdigest()


def chunk_point_id(text: str) -> str:
    """Детерминированный UUID-строка для точки Qdrant по содержимому чанка."""
    h = hashlib.sha256(_norm_text(text).encode("utf-8")).digest()
    return str(uuid.UUID(bytes=h[:16]))


class AnswerCache:
    def __init__(self, ttl_s=None, max_size=None):
        self.ttl = ttl_s if ttl_s is not None else settings.answer_cache_ttl_s
        self.max = max_size if max_size is not None else settings.answer_cache_max
        self._store: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(question, geo_filter="any", intent_hint=None) -> str:
        raw = f"{_norm_text(question)}|{geo_filter or 'any'}|{intent_hint or ''}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, question, geo_filter="any", intent_hint=None):
        if not settings.answer_cache_enabled:
            return None
        k = self.key(question, geo_filter, intent_hint)
        with self._lock:
            item = self._store.get(k)
            if not item:
                self.misses += 1
                return None
            ts, payload = item
            if time.time() - ts > self.ttl:
                del self._store[k]
                self.misses += 1
                return None
            self._store.move_to_end(k)  # LRU touch
            self.hits += 1
            return payload

    def set(self, question, payload, geo_filter="any", intent_hint=None):
        if not settings.answer_cache_enabled:
            return
        k = self.key(question, geo_filter, intent_hint)
        with self._lock:
            self._store[k] = (time.time(), payload)
            self._store.move_to_end(k)
            while len(self._store) > self.max:
                self._store.popitem(last=False)  # выселяем самый старый

    def stats(self):
        with self._lock:
            return {"size": len(self._store), "hits": self.hits,
                    "misses": self.misses, "ttl_s": self.ttl, "max": self.max}


# Модульный синглтон
answer_cache = AnswerCache()
