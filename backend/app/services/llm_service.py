"""LLMService: два провайдера — Ollama (локально) и Yandex AI Studio (OpenAI-совм.).

Выбор провайдера — settings.llm_provider ('ollama' | 'yandex').
  * ollama  — POST {ollama_url}/api/generate (стрим NDJSON + JSON-режим).
  * yandex  — POST {yandex_base_url}/chat/completions (OpenAI-совместимый SSE-стрим
              + response_format json_object), авторизация 'Authorization: Api-Key <key>'.

Роли моделей: synth (основной синтез) и tool (парсинг JSON / NER — дешевле/быстрее).
Ollama остаётся офлайн-фолбэком: если yandex недоступен, генерация вернёт понятную
ошибку, а сервис не упадёт.
"""

from __future__ import annotations

import json
import re

import httpx
from loguru import logger

from app.config import settings


def yandex_model_uri(model: str) -> str:
    """Короткое имя (yandexgpt/latest) → полный URI gpt://<folder>/<model>.

    Если передан уже полный URI (содержит '://') — возвращаем как есть
    (нужно для open-моделей: deepseek/qwen3/gpt-oss со своими схемами).
    """
    if not model:
        return model
    if "://" in model:
        return model
    return f"gpt://{settings.yandex_folder_id}/{model}"


def _parse_json_loose(raw: str):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)  # выдёргиваем первый JSON-объект
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


class LLMService:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=settings.llm_timeout_s)
        self.provider = (settings.llm_provider or "ollama").lower()

    async def close(self):
        await self.client.aclose()

    # ---- выбор модели по роли ----
    def _synth_model(self, model=None):
        if model:
            return model
        return settings.yandex_model_synth if self.provider == "yandex" else settings.ollama_model_synth

    def _tool_model(self, model=None):
        if model:
            return model
        return settings.yandex_model_tool if self.provider == "yandex" else settings.ollama_model_tool

    def _yandex_headers(self):
        return {
            "Authorization": f"Api-Key {settings.yandex_api_key}",
            "Content-Type": "application/json",
        }

    async def is_available(self):
        if not settings.llm_enabled:
            return False
        if self.provider == "yandex":
            return bool(settings.yandex_api_key and settings.yandex_folder_id)
        try:
            r = await self.client.get(f"{settings.ollama_url}/api/tags", timeout=3.0)
            return r.status_code == 200
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Ollama not reachable: {e}")
            return False

    # ==================================================================
    # Streaming
    # ==================================================================

    async def generate_stream(self, prompt, system=None, model=None):
        if not settings.llm_enabled:
            yield "[LLM отключён]"
            return
        model = self._synth_model(model)
        if self.provider == "yandex":
            async for tok in self._yandex_stream(prompt, system, model):
                yield tok
        else:
            async for tok in self._ollama_stream(prompt, system, model):
                yield tok

    async def _ollama_stream(self, prompt, system, model):
        body = {
            "model": model, "prompt": prompt, "stream": True,
            "options": {"temperature": settings.llm_temperature,
                        "num_predict": settings.llm_max_tokens},
        }
        if system:
            body["system"] = system
        try:
            async with self.client.stream(
                "POST", f"{settings.ollama_url}/api/generate", json=body
            ) as r:
                if r.status_code != 200:
                    text = await r.aread()
                    yield f"[Ошибка Ollama HTTP {r.status_code}: {text[:200]!r}]"
                    return
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "response" in obj:
                        yield obj["response"]
                    if obj.get("done"):
                        break
        except httpx.ConnectError:
            yield "[Ollama недоступен. Запустите `ollama serve` на хосте.]"
        except httpx.ReadTimeout:
            yield "[Тайм-аут генерации.]"
        except Exception as e:  # noqa: BLE001
            logger.exception("LLM stream error")
            yield f"[Ошибка генерации: {e}]"

    async def _yandex_stream(self, prompt, system, model):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {
            "model": yandex_model_uri(model), "messages": messages, "stream": True,
            "temperature": settings.llm_temperature, "max_tokens": settings.llm_max_tokens,
        }
        try:
            async with self.client.stream(
                "POST", f"{settings.yandex_base_url}/chat/completions",
                json=body, headers=self._yandex_headers(),
            ) as r:
                if r.status_code != 200:
                    text = await r.aread()
                    yield f"[Ошибка Yandex HTTP {r.status_code}: {text[:200]!r}]"
                    return
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or [{}]
                    piece = (choices[0].get("delta") or {}).get("content")
                    if piece:
                        yield piece
        except httpx.ConnectError:
            yield "[Yandex AI Studio недоступен (сеть).]"
        except httpx.ReadTimeout:
            yield "[Тайм-аут генерации.]"
        except Exception as e:  # noqa: BLE001
            logger.exception("Yandex stream error")
            yield f"[Ошибка генерации: {e}]"

    async def generate(self, prompt, system=None, model=None):
        chunks = []
        async for ch in self.generate_stream(prompt, system=system, model=model):
            chunks.append(ch)
        return "".join(chunks)

    # ==================================================================
    # JSON
    # ==================================================================

    async def generate_json(self, prompt, system=None, model=None, max_tokens=None):
        if not settings.llm_enabled:
            return None
        model = self._tool_model(model)
        if self.provider == "yandex":
            return await self._yandex_json(prompt, system, model, max_tokens)
        return await self._ollama_json(prompt, system, model, max_tokens)

    async def _ollama_json(self, prompt, system, model, max_tokens):
        body = {
            "model": model, "prompt": prompt, "stream": False, "format": "json",
            "options": {"temperature": 0.0, "num_predict": max_tokens or settings.llm_max_tokens},
        }
        if system:
            body["system"] = system
        try:
            r = await self.client.post(f"{settings.ollama_url}/api/generate", json=body)
            r.raise_for_status()
            raw = (r.json().get("response") or "").strip()
            return _parse_json_loose(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ollama generate_json failed: {e}")
            return None

    async def _yandex_json(self, prompt, system, model, max_tokens):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        base = {
            "model": yandex_model_uri(model), "messages": messages, "stream": False,
            "temperature": 0.0, "max_tokens": max_tokens or settings.llm_max_tokens,
        }
        for body in ({**base, "response_format": {"type": "json_object"}}, base):
            try:
                r = await self.client.post(
                    f"{settings.yandex_base_url}/chat/completions",
                    json=body, headers=self._yandex_headers(),
                )
                if r.status_code != 200:
                    logger.warning(f"yandex json HTTP {r.status_code}: {r.text[:200]}")
                    continue  # повтор без response_format (модель могла его отклонить)
                obj = r.json()
                raw = (((obj.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                parsed = _parse_json_loose(raw)
                if parsed is not None:
                    return parsed
            except Exception as e:  # noqa: BLE001
                logger.warning(f"yandex generate_json failed: {e}")
        return None
