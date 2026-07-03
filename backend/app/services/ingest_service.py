"""IngestService: загружаем корпус в Neo4j и Qdrant.

Этапы:
 1. Структурированные данные: эксперименты, справочники, сотрудники (опциональны)
 2. Вложенные архивы: рекурсивная распаковка (.zip/.rar/.001-.002)
 3. Документы: рекурсивный обход дерева → чанкинг → эмбеддинги + гео/тип
 4. Веб-ресурсы: trafilatura → чанки → эмбеддинги
 5. Auto-enrichment: NER + SIMILAR_TO + правила
"""

from __future__ import annotations

import hashlib

from loguru import logger

from app.config import settings
from app.db.neo4j_client import get_neo4j
from app.loaders import archives as arc_loader
from app.loaders import documents as doc_loader
from app.loaders import structured as struct_loader
from app.loaders import web as web_loader
from app.services import dictionary, geo


class IngestService:

    def load_corpus(self, corpus_dir):
        stats = {
            "experiments": 0, "documents": 0, "web_resources": 0,
            "chunks": 0, "chunks_deduped": 0, "archives": 0, "by_type": {}, "errors": [],
        }
        self._seen_chunk_hashes = set()
        self._chunks_deduped = 0

        # --- Структурированные данные (опциональны для реального корпуса) ---
        import pandas as pd
        try:
            data = struct_loader.load_corpus(corpus_dir)
        except Exception as e:
            # реальный корпус может не содержать experiments.csv / dicts —
            # это не повод падать: продолжаем с документами.
            stats["errors"].append(f"structured skipped: {e}")
            data = {"experiments": pd.DataFrame(),
                    "staff": pd.DataFrame(), "teams": pd.DataFrame()}

        self._upsert_dictionaries()
        self._upsert_teams(data["teams"])
        self._upsert_authors(data["staff"])

        for exp in struct_loader.iter_experiments(data["experiments"]):
            try:
                self._upsert_experiment(exp)
                stats["experiments"] += 1
            except Exception as e:
                stats["errors"].append(f"experiment {exp.get('experiment_id')}: {e}")

        # --- Вложенные архивы: разворачиваем перед обходом документов ---
        if settings.unpack_archives:
            try:
                arc_stats = arc_loader.unpack_recursive(corpus_dir)
                stats["archives"] = arc_stats.get("archives", 0)
            except Exception as e:
                stats["errors"].append(f"unpack failed: {e}")

        # --- Документы (рекурсивный обход реального дерева) ---
        for doc in doc_loader.iter_documents(corpus_dir):
            try:
                self._upsert_document(doc)
                chunks = list(doc_loader.iter_chunks(doc))
                if chunks:
                    self._index_chunks(chunks)
                    stats["chunks"] += len(chunks)
                stats["documents"] += 1
                dt = doc.get("doc_type") or "document"
                stats["by_type"][dt] = stats["by_type"].get(dt, 0) + 1
            except Exception as e:
                stats["errors"].append(f"document {doc.get('doc_id')}: {e}")

        # Веб-ресурсы
        for wdoc in web_loader.iter_web_resources(corpus_dir):
            try:
                self._upsert_document(wdoc)
                chunks = list(doc_loader.iter_chunks(wdoc))
                if chunks:
                    self._index_chunks(chunks)
                    stats["chunks"] += len(chunks)
                stats["web_resources"] += 1
            except Exception as e:
                stats["errors"].append(f"web {wdoc.get('doc_id')}: {e}")

        # Тематические связи между экспериментами
        try:
            self._index_experiments_embeddings()
            self._compute_similar_experiments()
        except Exception as e:
            stats["errors"].append(f"similarity: {e}")

        stats["chunks_deduped"] = self._chunks_deduped
        logger.info(
            f"Ingest done: {stats['experiments']} exp, {stats['documents']} doc, "
            f"{stats['web_resources']} web, {stats['chunks']} chunks, "
            f"{stats['archives']} archives, {len(stats['errors'])} errors"
        )
        return stats

    def _upsert_dictionaries(self):
        neo = get_neo4j()
        with neo.driver.session() as s:
            for m in dictionary.all_materials():
                s.execute_write(_tx_upsert_material, m)
            for p in dictionary.all_properties():
                s.execute_write(_tx_upsert_property, p)
            for mo in dictionary.all_modes():
                s.execute_write(_tx_upsert_mode, mo)
            for eq in dictionary.all_equipment():
                s.execute_write(_tx_upsert_equipment, eq)

    def _upsert_teams(self, teams_df):
        if teams_df is None or teams_df.empty:
            return
        neo = get_neo4j()
        with neo.driver.session() as s:
            for _, row in teams_df.iterrows():
                s.execute_write(_tx_upsert_team, dict(row))

    def _upsert_authors(self, staff_df):
        if staff_df is None or staff_df.empty:
            return
        neo = get_neo4j()
        with neo.driver.session() as s:
            for _, row in staff_df.iterrows():
                s.execute_write(_tx_upsert_author, dict(row))

    def _upsert_experiment(self, exp):
        neo = get_neo4j()
        with neo.driver.session() as s:
            s.execute_write(_tx_upsert_experiment, exp)
            for code in exp.get("material_codes") or []:
                s.execute_write(_tx_link_used_material, exp["experiment_id"], code)
            for code in exp.get("mode_codes") or []:
                s.execute_write(_tx_link_used_mode, exp["experiment_id"], code)
            for code in exp.get("equipment_codes") or []:
                s.execute_write(_tx_link_used_equipment, exp["experiment_id"], code)
            for aid in exp.get("author_ids") or []:
                s.execute_write(_tx_link_conducted_by, exp["experiment_id"], aid)
            for tg in exp.get("tag_codes") or []:
                s.execute_write(_tx_link_tag, exp["experiment_id"], tg)
            if exp.get("property_code"):
                s.execute_write(
                    _tx_link_measured, exp["experiment_id"],
                    exp["property_code"], exp.get("property_value"),
                    exp.get("property_unit"),
                )
            if exp.get("document_id"):
                s.execute_write(_tx_link_documented_in, exp["experiment_id"], exp["document_id"])
            if exp.get("conclusion_text"):
                conc_id = "CONC-" + hashlib.sha1(
                    (exp["experiment_id"] + exp["conclusion_text"]).encode("utf-8")
                ).hexdigest()[:12].upper()
                s.execute_write(_tx_upsert_conclusion, conc_id, exp["conclusion_text"], 0.85)
                s.execute_write(_tx_link_resulted_in, exp["experiment_id"], conc_id)

    def _upsert_document(self, doc):
        neo = get_neo4j()
        text_preview = " ".join(t for _, t in doc.get("pages", []))[:500]
        with neo.driver.session() as s:
            s.execute_write(_tx_upsert_document, doc, text_preview)

    def _index_chunks(self, chunks):
        from app.services.rag_service import RAGService
        from app.db.qdrant_client import get_qdrant
        from app.config import settings
        from app.services import cache_service
        from qdrant_client.http.models import PointStruct

        # CAG-дедуп: одинаковые чанки (boilerplate журналов) не эмбеддим повторно;
        # детерминированный point-id схлопывает дубли в одну точку Qdrant.
        seen = getattr(self, "_seen_chunk_hashes", None)
        if seen is None:
            seen = self._seen_chunk_hashes = set()
        fresh = []
        for c in chunks:
            if settings.chunk_dedup:
                h = cache_service.chunk_content_hash(c["text"])
                c["_pid"] = cache_service.chunk_point_id(c["text"])
                if h in seen:
                    self._chunks_deduped = getattr(self, "_chunks_deduped", 0) + 1
                    continue
                seen.add(h)
            else:
                c["_pid"] = c["chunk_id"]
            fresh.append(c)
        if not fresh:
            return
        rag = RAGService()
        vecs = rag.embed_texts([c["text"] for c in fresh])
        points = [
            PointStruct(
                id=c["_pid"], vector=vec,
                payload={"doc_id": c["doc_id"], "page": c["page"], "text": c["text"][:1000]},
            )
            for c, vec in zip(fresh, vecs)
        ]
        get_qdrant().upsert(collection_name=settings.qdrant_collection_chunks, points=points)

    def _index_experiments_embeddings(self):
        from app.services.rag_service import RAGService
        from app.db.qdrant_client import get_qdrant
        from app.config import settings
        neo = get_neo4j()
        with neo.driver.session() as s:
            recs = s.run(
                "MATCH (e:Experiment) RETURN e.experiment_id AS id, "
                "e.title AS title, e.description AS description"
            )
            rows = [dict(r) for r in recs]
        if not rows:
            return
        rag = RAGService()
        texts = [(r.get("title") or "") + " " + (r.get("description") or "") for r in rows]
        vecs = rag.embed_texts(texts)
        from qdrant_client.http.models import PointStruct
        points = [
            PointStruct(
                id=rag._stable_point_id(r["id"]), vector=vec,
                payload={"experiment_id": r["id"], "title": r.get("title", "")},
            )
            for r, vec in zip(rows, vecs)
        ]
        get_qdrant().upsert(collection_name=settings.qdrant_collection_experiments, points=points)
        logger.info(f"Indexed {len(points)} experiments in Qdrant")

    def _compute_similar_experiments(self, top_k=8):
        from app.services.rag_service import RAGService
        from app.db.qdrant_client import get_qdrant
        from app.config import settings
        neo = get_neo4j()
        q = get_qdrant()
        with neo.driver.session() as s:
            ids = [r["id"] for r in s.run("MATCH (e:Experiment) RETURN e.experiment_id AS id")]
        rag = RAGService()
        added = 0
        for exp_id in ids:
            pid = rag._stable_point_id(exp_id)
            try:
                pts = q.retrieve(
                    collection_name=settings.qdrant_collection_experiments,
                    ids=[pid], with_vectors=True,
                )
            except Exception:
                continue
            if not pts:
                continue
            vec = pts[0].vector
            results = q.search(
                collection_name=settings.qdrant_collection_experiments,
                query_vector=vec, limit=top_k + 1,
                score_threshold=settings.similarity_threshold,
            )
            with neo.driver.session() as s:
                for r in results:
                    other_id = (r.payload or {}).get("experiment_id")
                    if not other_id or other_id == exp_id:
                        continue
                    weight = max(0.5, 1.5 / max(float(r.score), 0.01))
                    s.execute_write(_tx_link_similar, exp_id, other_id, float(r.score), weight)
                    added += 1
        logger.info(f"Added {added} SIMILAR_TO edges")


# ============= Cypher tx =============


def _tx_upsert_material(tx, m):
    tx.run("""
        MERGE (x:Material {code: $code})
        SET x.display_name = $name, x.aliases = $aliases,
            x.family = $family, x.base_element = $base_element, x.gost = $gost
        """,
        code=m["code"], name=m["display_name"],
        aliases=" ".join(m.get("aliases", [])),
        family=m.get("meta", {}).get("family"),
        base_element=m.get("meta", {}).get("base_element"),
        gost=m.get("meta", {}).get("gost"))


def _tx_upsert_property(tx, p):
    tx.run("""
        MERGE (x:Property {code: $code})
        SET x.display_name = $name, x.aliases = $aliases,
            x.unit = $unit, x.category = $category
        """,
        code=p["code"], name=p["display_name"],
        aliases=" ".join(p.get("aliases", [])),
        unit=p.get("meta", {}).get("unit"),
        category=p.get("meta", {}).get("category"))


def _tx_upsert_mode(tx, mo):
    parsed = _parse_mode_safe(mo["display_name"])
    tx.run("""
        MERGE (x:Mode {code: $code})
        SET x.display_name = $name, x.category = $category,
            x.temperature_c = $temp, x.duration_h = $dur
        """,
        code=mo["code"], name=mo["display_name"],
        category=mo.get("meta", {}).get("category"),
        temp=parsed.get("temperature_c") if parsed else None,
        dur=parsed.get("duration_h") if parsed else None)
    if parsed:
        for name, val, unit in _flatten_mode_params(parsed):
            tx.run("""
                MATCH (m:Mode {code: $mcode})
                MERGE (p:ModeParam {name: $name, unit: $unit, value: $value})
                MERGE (m)-[:HAS_PARAM]->(p)
                """,
                mcode=mo["code"], name=name, unit=unit, value=val)


def _tx_upsert_equipment(tx, eq):
    tx.run("""
        MERGE (x:Equipment {code: $code})
        SET x.display_name = $name, x.type = $type
        """,
        code=eq["code"], name=eq["display_name"],
        type=eq.get("meta", {}).get("type"))


def _tx_upsert_team(tx, row):
    tx.run("""
        MERGE (t:Team {team_id: $id})
        SET t.display_name = $name, t.lab_code = $lab
        """,
        id=str(row.get("team_id") or "").strip(),
        name=str(row.get("display_name") or row.get("team_id") or ""),
        lab=str(row.get("lab_code") or "") or None)


def _tx_upsert_author(tx, row):
    aid = str(row.get("author_id") or "").strip()
    if not aid:
        return
    tx.run("""
        MERGE (a:Author {author_id: $id})
        SET a.full_name = $name
        """, id=aid, name=str(row.get("full_name") or aid))
    team_id = str(row.get("team_id") or "").strip()
    if team_id:
        tx.run("""
            MATCH (a:Author {author_id: $aid}), (t:Team {team_id: $tid})
            MERGE (a)-[:MEMBER_OF]->(t)
            """, aid=aid, tid=team_id)


def _tx_upsert_experiment(tx, e):
    tx.run("""
        MERGE (x:Experiment {experiment_id: $id})
        SET x.title = $title, x.description = $description,
            x.year = $year, x.date = $date
        """,
        id=e["experiment_id"], title=e.get("title"),
        description=e.get("description"), year=e.get("year"), date=e.get("date"))


def _tx_upsert_document(tx, doc, summary):
    tx.run("""
        MERGE (d:Document {doc_id: $id})
        SET d.title = $title, d.summary = $summary, d.file_path = $path,
            d.language = $lang, d.country_code = $country, d.geo_region = $region,
            d.kind = $kind, d.format = $format, d.doc_type = $doc_type,
            d.source_category = $source_category, d.journal = $journal,
            d.year = $year, d.page_count = $page_count, d.last_fetched = datetime()
        """,
        id=doc["doc_id"], title=doc.get("title", ""), summary=(summary or "")[:2000],
        path=doc.get("file_path"), lang=doc.get("language"),
        country=doc.get("country_code"), region=doc.get("geo_region"),
        kind=doc.get("kind", "file"), format=doc.get("format"),
        doc_type=doc.get("doc_type"), source_category=doc.get("source_category"),
        journal=doc.get("journal"), year=doc.get("year"),
        page_count=doc.get("page_count"))


def _tx_upsert_conclusion(tx, conc_id, text, confidence):
    tx.run("""
        MERGE (c:Conclusion {conclusion_id: $id})
        SET c.text = $text, c.confidence = $conf, c.last_updated = datetime(),
            c.version = coalesce(c.version, 1)
        """, id=conc_id, text=text[:2000], conf=confidence)


def _tx_link_used_material(tx, exp_id, code):
    tx.run("""
        MATCH (e:Experiment {experiment_id: $eid}), (m:Material {code: $code})
        MERGE (e)-[r:USED_MATERIAL]->(m) SET r.weight = 0.5
        """, eid=exp_id, code=code)


def _tx_link_used_mode(tx, exp_id, code):
    tx.run("""
        MATCH (e:Experiment {experiment_id: $eid}), (m:Mode {code: $code})
        MERGE (e)-[r:USED_MODE]->(m) SET r.weight = 0.5
        """, eid=exp_id, code=code)


def _tx_link_used_equipment(tx, exp_id, code):
    tx.run("""
        MATCH (e:Experiment {experiment_id: $eid}), (eq:Equipment {code: $code})
        MERGE (e)-[r:USED_EQUIPMENT]->(eq) SET r.weight = 0.5
        """, eid=exp_id, code=code)


def _tx_link_conducted_by(tx, exp_id, author_id):
    tx.run("""
        MATCH (e:Experiment {experiment_id: $eid}), (a:Author {author_id: $aid})
        MERGE (e)-[r:CONDUCTED_BY]->(a) SET r.weight = 0.8
        """, eid=exp_id, aid=author_id)


def _tx_link_tag(tx, exp_id, tag_code):
    tx.run("""
        MERGE (tg:Tag {code: $code})
        WITH tg
        MATCH (e:Experiment {experiment_id: $eid})
        MERGE (e)-[r:TAGGED_WITH]->(tg) SET r.weight = 2.0
        """, code=tag_code, eid=exp_id)


def _tx_link_measured(tx, exp_id, prop_code, value, unit):
    tx.run("""
        MATCH (e:Experiment {experiment_id: $eid}), (p:Property {code: $code})
        MERGE (e)-[r:MEASURED]->(p)
        SET r.value = $value, r.unit = $unit, r.weight = 0.5
        """, eid=exp_id, code=prop_code, value=value, unit=unit)


def _tx_link_documented_in(tx, exp_id, doc_id):
    tx.run("""
        MATCH (e:Experiment {experiment_id: $eid}), (d:Document {doc_id: $did})
        MERGE (e)-[r:DOCUMENTED_IN]->(d) SET r.weight = 1.0
        """, eid=exp_id, did=doc_id)


def _tx_link_resulted_in(tx, exp_id, conc_id):
    tx.run("""
        MATCH (e:Experiment {experiment_id: $eid}), (c:Conclusion {conclusion_id: $cid})
        MERGE (e)-[r:RESULTED_IN]->(c) SET r.weight = 0.4
        """, eid=exp_id, cid=conc_id)


def _tx_link_similar(tx, src, dst, score, weight):
    tx.run("""
        MATCH (a:Experiment {experiment_id: $src}), (b:Experiment {experiment_id: $dst})
        WHERE elementId(a) < elementId(b)
        MERGE (a)-[r:SIMILAR_TO]->(b)
        SET r.score = $score, r.weight = $weight
        """, src=src, dst=dst, score=score, weight=weight)


def _parse_mode_safe(text):
    try:
        return struct_loader.parse_mode_string(text)
    except Exception:
        return None


def _flatten_mode_params(parsed):
    out = []
    if "temperature_c" in parsed:
        out.append(("temperature", float(parsed["temperature_c"]), "°C"))
    if "duration_h" in parsed:
        out.append(("duration", float(parsed["duration_h"]), "h"))
    # расширенные параметры (см. Gap #2)
    for key, unit in (
        ("concentration_mgl", "mg/l"), ("flow_rate_m3h", "m^3/h"),
        ("pressure_mpa", "MPa"), ("ph_value", "pH"),
        ("current_density_am2", "A/m^2"), ("cost_rub", "RUB"),
        ("throughput_tday", "t/day"),
    ):
        if key in parsed:
            name = key.rsplit("_", 1)[0]
            out.append((name, float(parsed[key]), unit))
    if "pressure" in parsed:
        import re
        m = re.match(r"([\d.]+)\s*(\S+)", str(parsed["pressure"]))
        if m:
            try:
                out.append(("pressure", float(m.group(1)), m.group(2)))
            except ValueError:
                pass
    return out
