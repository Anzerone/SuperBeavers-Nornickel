"""API: POST /api/v1/ask — Q&A со стримом.

Дополнительные фильтры: geo_filter (any/domestic/foreign), min_confidence.
"""

from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from app.services.graph_matcher import GraphMatcher
from app.services.graph_service import GraphService
from app.services.llm_service import LLMService
from app.services.query_parser import QueryParser
from app.services.rag_service import RAGService
from app.services.synthesizer import Synthesizer
from app.services.cache_service import answer_cache
from app.services.graph_service import fulltext_seed
from app.config import settings

router = APIRouter()

# Кэш ответов для последующего экспорта
_ANSWER_CACHE: dict = {}


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=1000)
    expand_query: bool = True
    geo_filter: str = "any"       # any | domestic | foreign
    min_confidence: float | None = None
    intent_hint: str | None = None  # "literature_review" | "comparison" | ...
    answer_id: str | None = None


def _sse(event, data):
    payload = json.dumps(data, ensure_ascii=False).replace("\n", "\\n")
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("")
async def ask(req: AskRequest):
    llm = LLMService()
    rag = RAGService()
    parser = QueryParser(llm)
    matcher = GraphMatcher()
    graph = GraphService()
    synth = Synthesizer(llm, rag)

    async def event_stream():
        try:
            cached = answer_cache.get(req.question, req.geo_filter, req.intent_hint)
            if cached:
                yield _sse("intent", cached.get("intent") or {})
                yield _sse("match", cached.get("match_meta") or {"count": len(cached.get("experiments") or [])})
                yield _sse("subgraph", cached.get("subgraph") or {"nodes": [], "edges": []})
                yield _sse("sources", cached.get("sources") or [])
                yield _sse("cache", {"hit": True})
                yield _sse("token", {"text": cached.get("answer") or ""})
                if req.answer_id:
                    _ANSWER_CACHE[req.answer_id] = cached
                yield _sse("done", {"experiments_shown": len(cached.get("experiments") or []), "cached": True})
                await llm.close()
                return

            intent = await parser.parse(req.question)
            if req.intent_hint:
                intent["intent"] = req.intent_hint
            yield _sse("intent", intent)

            match_result = matcher.match(
                intent, geo_filter=req.geo_filter,
                min_confidence=req.min_confidence,
            )
            experiments = match_result["experiments"]
            match_meta = {
                "count": len(experiments),
                "regions": match_result.get("regions_seen"),
                "cypher": match_result["cypher"],
            }
            yield _sse("match", match_meta)

            if req.expand_query and len(experiments) < 3:
                yield _sse("info", {"msg": "мало результатов — расширяем через семантику"})
                sem = rag.search_similar_experiments(req.question, top_k=15)
                exp_ids = {e["experiment_id"] for e in experiments}
                for r in sem:
                    if r["experiment_id"] not in exp_ids:
                        experiments.append({
                            "experiment_id": r["experiment_id"],
                            "title": r["title"], "materials": [], "modes": [],
                            "property": None, "value": None, "unit": None,
                            "doc_id": None, "geo_region": "other",
                        })
                        exp_ids.add(r["experiment_id"])

                # FTS-сидинг (лексический recall по кодам/точным терминам)
                if settings.fts_seed_enabled:
                    seed = fulltext_seed(req.question)
                    for eid in seed["experiments"]:
                        if eid and eid not in exp_ids:
                            experiments.append({
                                "experiment_id": eid, "title": None, "materials": [],
                                "modes": [], "property": None, "value": None, "unit": None,
                                "doc_id": None, "geo_region": "other",
                            })
                            exp_ids.add(eid)
                    # документы без эксперимента — как источники для синтеза
                    have_docs = {e.get("doc_id") for e in experiments}
                    for did in seed["doc_ids"]:
                        if did and did not in have_docs:
                            experiments.append({
                                "experiment_id": None, "title": None, "materials": [],
                                "modes": [], "property": None, "value": None, "unit": None,
                                "doc_id": did, "geo_region": "other", "_fts_doc": True,
                            })
                            have_docs.add(did)

            exp_ids = [e["experiment_id"] for e in experiments if e.get("experiment_id")]
            sub = graph.fetch_for_experiments(exp_ids) if exp_ids else {"nodes": [], "edges": []}
            yield _sse("subgraph", sub)

            chunks_used, token_stream = await synth.synthesize_stream(
                req.question, intent, experiments,
            )
            # Верификация источников (Gap #6): обогащаем чанки метаданными документа
            try:
                metas = graph.fetch_document_meta([c.get("doc_id") for c in chunks_used])
                for c in chunks_used:
                    m = metas.get(c.get("doc_id"))
                    if m:
                        for k in ("title", "doc_type", "journal", "year",
                                  "geo_region", "country_code", "last_fetched"):
                            c[k] = m.get(k)
            except Exception as e:
                logger.warning(f"source meta enrich failed: {e}")
            yield _sse("sources", chunks_used)

            full_answer_parts = []
            async for tok in token_stream():
                full_answer_parts.append(tok)
                yield _sse("token", {"text": tok})

            full_answer = "".join(full_answer_parts)

            # Кэшируем ответ: для экспорта (по answer_id) и CAG (по вопросу)
            answer_payload = {
                "question": req.question,
                "intent": intent,
                "geo_filter": req.geo_filter,
                "experiments": experiments,
                "sources": chunks_used,
                "answer": full_answer,
                "subgraph": sub,
                "match_meta": match_meta,
            }
            if req.answer_id:
                _ANSWER_CACHE[req.answer_id] = answer_payload
            if full_answer.strip():
                answer_cache.set(req.question, answer_payload, req.geo_filter, req.intent_hint)

            yield _sse("done", {"experiments_shown": len(experiments)})
        except Exception as e:
            logger.exception("ask failed")
            yield _sse("error", {"message": str(e)})
        finally:
            await llm.close()

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def get_answer(answer_id):
    return _ANSWER_CACHE.get(answer_id)
