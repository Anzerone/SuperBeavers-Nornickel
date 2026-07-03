"""GraphService: чтение подграфов из Neo4j вокруг произвольных сущностей."""

from __future__ import annotations

from loguru import logger

from app.config import settings
from app.db.neo4j_client import get_neo4j

# Маппинг Neo4j-label → upper-case type для фронта
LABEL_TO_TYPE = {
    "Material": "material",
    "Property": "property",
    "Mode": "mode",
    "ModeParam": "mode_param",
    "Equipment": "equipment",
    "Experiment": "experiment",
    "Conclusion": "conclusion",
    "Document": "document",
    "Author": "author",
    "Team": "team",
    "Tag": "tag",
}


class GraphService:

    def fetch_around(self, label, code, depth=2, max_nodes=None):
        """Подграф вокруг одного узла (любого типа).

        label — 'Material' / 'Experiment' / ... ; code — значение ключа узла.
        """
        max_nodes = max_nodes or settings.graph_max_nodes
        key_field = _key_field_for_label(label)
        if not key_field:
            return {"nodes": [], "edges": []}
        neo = get_neo4j()
        with neo.driver.session() as s:
            return s.execute_read(
                _tx_around_one, label, key_field, code, depth, max_nodes
            )

    def fetch_document_meta(self, doc_ids):
        """Метаданные документов для верификации источников на экране ответа:
        тип, издание, год, география, дата актуализации."""
        ids = [d for d in dict.fromkeys(doc_ids) if d]
        if not ids:
            return {}
        neo = get_neo4j()
        with neo.driver.session() as s:
            rows = list(s.run(
                """
                MATCH (d:Document) WHERE d.doc_id IN $ids
                RETURN d.doc_id AS doc_id, d.title AS title, d.doc_type AS doc_type,
                       d.journal AS journal, d.year AS year, d.geo_region AS geo_region,
                       d.country_code AS country_code, d.last_fetched AS last_fetched,
                       d.file_path AS file_path
                """, ids=ids,
            ))
        out = {}
        for r in rows:
            out[r["doc_id"]] = {
                "title": r["title"], "doc_type": r["doc_type"], "journal": r["journal"],
                "year": r["year"], "geo_region": r["geo_region"],
                "country_code": r["country_code"], "last_fetched": _iso(r["last_fetched"]),
                "file_path": r["file_path"],
            }
        return out

    def fetch_for_experiments(self, experiment_ids, max_nodes=None):
        """Подграф для набора экспериментов + их прямых соседей.

        Используется для UI ответа на Q&A — рисуем эксперименты, найденные
        матчером, плюс их материалы/режимы/свойства/документы.
        """
        max_nodes = max_nodes or settings.graph_max_nodes
        if not experiment_ids:
            return {"nodes": [], "edges": []}
        neo = get_neo4j()
        with neo.driver.session() as s:
            return s.execute_read(_tx_around_experiments, experiment_ids, max_nodes)


class _GraphExtras:
    pass


def _lucene_escape(text):
    """Экранирует спецсимволы Lucene и собирает OR-запрос из слов вопроса."""
    import re as _re
    words = [w for w in _re.split(r"[^0-9A-Za-zА-Яа-яЁё]+", text or "") if len(w) >= 3]
    esc = []
    for w in words[:12]:
        esc.append(_re.sub(r'([+\-!(){}\[\]^"~*?:\\/])', r"\\\1", w))
    return " OR ".join(esc) if esc else (text or "").strip()


def fulltext_seed(query_text, limit=None):
    """FTS-сидинг ретрива (Neo4j full-text). Возвращает experiment_ids + doc_ids,
    найденные лексически по названиям сущностей и тексту документов —
    повышает recall там, где эмбеддинги промахиваются (коды, точные термины)."""
    from app.config import settings as _st
    limit = limit or _st.fts_seed_limit
    q = _lucene_escape(query_text)
    if not q:
        return {"experiments": [], "doc_ids": []}
    neo = get_neo4j()
    exp_ids, doc_ids = [], []
    try:
        with neo.driver.session() as s:
            for rec in s.run(
                """
                CALL db.index.fulltext.queryNodes('document_search', $q) YIELD node, score
                WITH node, score ORDER BY score DESC LIMIT $lim
                OPTIONAL MATCH (e:Experiment)-[:DOCUMENTED_IN]->(node)
                RETURN node.doc_id AS doc_id, collect(e.experiment_id) AS exps
                """, q=q, lim=limit,
            ):
                if rec.get("doc_id"):
                    doc_ids.append(rec["doc_id"])
                for eid in rec.get("exps") or []:
                    if eid:
                        exp_ids.append(eid)
            for rec in s.run(
                """
                CALL db.index.fulltext.queryNodes('experiment_search', $q) YIELD node, score
                RETURN node.experiment_id AS eid ORDER BY score DESC LIMIT $lim
                """, q=q, lim=limit,
            ):
                if rec.get("eid"):
                    exp_ids.append(rec["eid"])
    except Exception as e:  # noqa: BLE001 — FTS не должен ронять ответ
        logger.warning(f"fulltext_seed failed: {e}")
    # dedup, сохраняя порядок
    return {
        "experiments": list(dict.fromkeys(exp_ids))[: limit * 2],
        "doc_ids": list(dict.fromkeys(doc_ids))[: limit * 2],
    }


def graph_stats():
    """Сводная статистика графа для /stats-дашборда (janson-заимствование)."""
    neo = get_neo4j()
    out = {"nodes": {}, "edges": {}, "documents_by_type": {}, "geo": {}, "totals": {}}
    try:
        with neo.driver.session() as s:
            for r in s.run("MATCH (n) UNWIND labels(n) AS l RETURN l AS l, count(*) AS c"):
                out["nodes"][r["l"]] = r["c"]
            for r in s.run("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c"):
                out["edges"][r["t"]] = r["c"]
            for r in s.run("MATCH (d:Document) RETURN coalesce(d.doc_type,'—') AS t, count(*) AS c"):
                out["documents_by_type"][r["t"]] = r["c"]
            for r in s.run("MATCH (d:Document) RETURN coalesce(d.geo_region,'other') AS g, count(*) AS c"):
                out["geo"][r["g"]] = r["c"]
            rec = s.run(
                "MATCH (e:Experiment) RETURN count(e) AS exp, "
                "size([(x:Experiment) WHERE x.extracted | x]) AS extracted"
            ).single()
            if rec:
                out["totals"]["experiments"] = rec["exp"]
                out["totals"]["experiments_extracted"] = rec["extracted"]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"graph_stats failed: {e}")
        out["error"] = str(e)[:200]
    # чанки — из Qdrant
    try:
        from app.db.qdrant_client import get_qdrant
        from app.config import settings as _st
        cnt = get_qdrant().count(collection_name=_st.qdrant_collection_chunks, exact=True)
        out["totals"]["chunks"] = getattr(cnt, "count", None)
    except Exception:  # noqa: BLE001
        out["totals"]["chunks"] = None
    return out


def _key_field_for_label(label):
    return {
        "Material": "code",
        "Property": "code",
        "Mode": "code",
        "Equipment": "code",
        "Tag": "code",
        "Experiment": "experiment_id",
        "Author": "author_id",
        "Team": "team_id",
        "Document": "doc_id",
        "Conclusion": "conclusion_id",
    }.get(label)


def _tx_around_one(tx, label, key_field, code, depth, max_nodes):
    q = f"""
    MATCH (anchor:{label} {{{key_field}: $code}})
    CALL {{
      WITH anchor
      MATCH (anchor)-[*0..{depth}]-(n)
      WHERE NOT n:ModeParam
      RETURN DISTINCT n
      LIMIT $max
    }}
    RETURN DISTINCT n
    LIMIT $max
    """
    nodes_raw = [r["n"] for r in tx.run(q, code=code, max=max_nodes)]
    return _pack_subgraph(tx, nodes_raw, anchor_keys={code})


def _tx_around_experiments(tx, exp_ids, max_nodes):
    q = """
    MATCH (e:Experiment) WHERE e.experiment_id IN $ids
    CALL {
      WITH e
      MATCH (e)-[*0..1]-(n)
      WHERE NOT n:ModeParam
      RETURN DISTINCT n
      LIMIT $max
    }
    RETURN DISTINCT n
    LIMIT $max
    """
    nodes_raw = [r["n"] for r in tx.run(q, ids=exp_ids, max=max_nodes)]
    return _pack_subgraph(tx, nodes_raw, anchor_keys=set(exp_ids))


def _pack_subgraph(tx, nodes_raw, anchor_keys):
    """Превращает Neo4j-узлы и их рёбра в плоский Cytoscape-формат."""
    if not nodes_raw:
        return {"nodes": [], "edges": []}

    node_ids_internal = [n.element_id for n in nodes_raw]

    edges_res = tx.run(
        """
        MATCH (a)-[r]->(b)
        WHERE elementId(a) IN $ids AND elementId(b) IN $ids
        RETURN a, r, b, type(r) AS rel_type, properties(r) AS props
        """,
        ids=node_ids_internal,
    )
    edges = []
    for rec in edges_res:
        a, b = rec["a"], rec["b"]
        props = rec["props"] or {}
        a_id, _ = _node_key(a)
        b_id, _ = _node_key(b)
        if not a_id or not b_id:
            continue
        edges.append({
            "source": a_id,
            "target": b_id,
            "type": rec["rel_type"],
            "weight": float(props.get("weight") or 1.0),
            "value": props.get("value"),
            "unit": props.get("unit"),
            "score": props.get("score"),
        })

    nodes = []
    for n in nodes_raw:
        labels = list(n.labels)
        nid, label = _node_key(n)
        if not nid:
            continue
        node_type = LABEL_TO_TYPE.get(label, "unknown")
        title = (
            n.get("display_name")
            or n.get("title")
            or n.get("full_name")
            or n.get("text")
            or nid
        )
        nodes.append({
            "id": nid,
            "type": node_type,
            "label": label,
            "title": title,
            "short_title": _truncate(title, 80),
            "year": n.get("year"),
            "date": n.get("date"),
            "is_anchor": nid in anchor_keys,
            # тип-специфичные поля
            "unit": n.get("unit"),
            "category": n.get("category"),
            "family": n.get("family"),
            "temperature_c": n.get("temperature_c"),
            "duration_h": n.get("duration_h"),
            "summary": n.get("summary"),
            "file_path": n.get("file_path"),
            "text": n.get("text"),
            # === Верификация фактов (Gap #6): источник + достоверность + дата ===
            "confidence": n.get("confidence"),
            "last_updated": _iso(n.get("last_updated")),
            "last_fetched": _iso(n.get("last_fetched")),
            "geo_region": n.get("geo_region"),
            "country_code": n.get("country_code"),
            "doc_type": n.get("doc_type"),
            "journal": n.get("journal"),
            "source_doc_id": n.get("source_doc_id"),
            "extracted": bool(n.get("extracted")) if n.get("extracted") is not None else None,
            "page_count": n.get("page_count"),
        })
    _attach_confirmations(tx, nodes)
    return {"nodes": nodes, "edges": edges}


def _iso(v):
    """Neo4j DateTime/Date → ISO-строка (JSON-safe)."""
    if v is None:
        return None
    try:
        return v.isoformat()
    except AttributeError:
        return str(v)


def _attach_confirmations(tx, nodes):
    """Для Conclusion добавляет число подтверждений/опровержений.

    confirmation_count = сколько экспериментов привели к выводу (:RESULTED_IN)
    плюс сколько других выводов его подтверждают (:CONFIRMS).
    contradicts_count = сколько выводов ему противоречат (:CONTRADICTS).
    """
    ids = [n["id"] for n in nodes if n.get("label") == "Conclusion"]
    if not ids:
        return
    try:
        rows = list(tx.run(
            """
            MATCH (c:Conclusion) WHERE c.conclusion_id IN $ids
            RETURN c.conclusion_id AS id,
                   size([(e:Experiment)-[:RESULTED_IN]->(c) | e]) AS supported_by,
                   size([(x:Conclusion)-[:CONFIRMS]->(c) | x]) AS confirms,
                   size([(y:Conclusion)-[:CONTRADICTS]->(c) | y]) AS contradicts
            """,
            ids=ids,
        ))
    except Exception:  # noqa: BLE001 — верификация не должна ломать подграф
        return
    by_id = {r["id"]: r for r in rows}
    for n in nodes:
        r = by_id.get(n["id"])
        if not r:
            continue
        n["confirmation_count"] = int(r["supported_by"]) + int(r["confirms"])
        n["contradicts_count"] = int(r["contradicts"])


def _node_key(n):
    """Возвращает (внешний_id, primary_label) для любого узла."""
    labels = list(n.labels)
    for label in (
        "Experiment", "Material", "Property", "Mode", "Equipment",
        "Author", "Team", "Document", "Conclusion", "Tag", "ModeParam",
    ):
        if label not in labels:
            continue
        key_field = _key_field_for_label(label)
        if key_field:
            val = n.get(key_field)
            if val:
                return val, label
        # ModeParam: композитный ключ
        if label == "ModeParam":
            return f"{n.get('name')}={n.get('value')}{n.get('unit') or ''}", label
    return None, None


def _truncate(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"
