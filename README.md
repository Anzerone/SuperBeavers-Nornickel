<div align="center">

# 🪢 Научный клубок

**Knowledge graph + Q&A + Self-Expanding система для R&D-корпуса**

Решение для трека «Научный клубок» — НОРНИКЕЛЬ AI SCIENCE HACK 2026

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.11-blue)](https://python.org)
[![Next.js](https://img.shields.io/badge/Next.js-15-black)](https://nextjs.org)
[![Neo4j](https://img.shields.io/badge/Neo4j-5-blue)](https://neo4j.com)
[![Qdrant](https://img.shields.io/badge/Qdrant-latest-red)](https://qdrant.tech)

</div>

---

## 🎯 Что это

Локальная система, которая:

- **Отвечает на вопросы** вида «что уже делали по материалу X при режиме Y и каков эффект на свойство Z» — с прямыми ссылками на эксперименты и документы.
- **Различает отечественную и зарубежную практику** (гео-фильтр РФ/СНГ / зарубеж / всё).
- **Понимает числовые диапазоны:** температура, концентрация, расход, давление, pH, плотность тока, экономические показатели, производительность.
- **Строит литобзоры** с группировкой по методу, году, географии + консенсус / разногласия.
- **Сравнивает варианты** (материалы, режимы, технологии) по параметрам с разбросом.
- **Показывает пробелы** — что комбинации (материал × режим × свойство) ещё не изучены (тепловая карта + link prediction).
- **Ведёт историю значений** свойства во времени с автоматическими поворотными точками.
- **Экспортирует ответ** в Markdown, JSON-LD (FAIR-совместимый) и PDF.
- **Самообогащается** — при добавлении нового документа автоматически создаёт связи через NER, семантику и правила.
- **Версионирует факты** (SUPERSEDED_BY) и логирует все действия (audit_log).
- **4 ролевые модели** (researcher / analyst / manager / admin) с JWT-авторизацией.

---

## 📦 Стек

| Слой | Технология |
|------|-----------|
| **LLM (локально)** | Ollama · Qwen 2.5 14B Q5 (synth) + Qwen 2.5 3B Q5 (tool) |
| **Эмбеддинги** | BGE-M3 (мультиязычные, 1024-мерные) через fastembed |
| **Knowledge Graph** | Neo4j 5 Community + APOC + GDS (Node2Vec, Louvain, PageRank, Link Prediction) |
| **Векторный поиск** | Qdrant (2 коллекции: описания экспериментов + чанки документов) |
| **Backend** | Python 3.11 · FastAPI · pdfplumber · python-docx · trafilatura · rapidfuzz · reportlab |
| **Frontend** | Next.js 15 (JS) · Cytoscape.js · Chart.js · Tailwind CSS |
| **Auth** | JWT (HS256) + PBKDF2 + SQLite audit log |

**Ограничения соблюдены:** Anthropic/OpenAI не используются, приоритет — экономия ресурсов (14B на GPU, 3B на батчи).

---

## 🚀 Быстрый старт

### Предусловия

1. **Docker Desktop** запущен.
2. **24 ГБ RAM** (Neo4j 4 + Qdrant 1 + Ollama 12 + Windows + бэк/фронт).
3. **NVIDIA GPU** с CUDA (рекомендуется RTX 4090 / 5090).
4. **[Ollama](https://ollama.com/download)** установлена.

### Установка

```powershell
git clone https://github.com/YOUR_ORG/scientific-tangle.git
cd scientific-tangle
copy .env.example .env

# Один раз — модели (суммарно ~12 ГБ)
ollama serve
ollama pull qwen2.5:14b-instruct-q5_K_M
ollama pull qwen2.5:3b-instruct-q5_K_M

# Поднимаем стек
docker compose up --build
```

Первый запуск: 10–20 минут (образы + Neo4j APOC/GDS + BGE-M3 кэш).

### Проверка

- **Frontend:** <http://localhost:3000>
- **API:** <http://localhost:8000/docs>
- **Neo4j Browser:** <http://localhost:7474> (neo4j / changeme123)
- **Qdrant UI:** <http://localhost:6333/dashboard>

### Формат корпуса

Система принимает **реальный корпус как дерево сырых документов** — структурированные
`experiments.csv` / словари опциональны. Ожидаемая раскладка (папка `data/corpus/`):

```
data/corpus/
└── Источники информации/
    ├── Доклады/…                    → doc_type = report
    ├── Журналы/<Издание>/<Год>/…    → doc_type = journal (+ journal, year из пути)
    ├── Статьи/…                     → doc_type = article
    ├── Обзоры/…                     → doc_type = review
    └── Материалы конференций/…      → doc_type = conference
```

- **Форматы:** PDF · DOCX · DOC · PPTX · PPT · XLSX · XLS · TXT (старые бинарные
  .doc/.ppt/.xls — через libreoffice, если установлен).
- **Вложенные архивы** (.zip / .rar / многотомные .001-.002) распаковываются
  **рекурсивно** перед обходом; русские имена в cp866 декодируются корректно.
- **Метаданные из пути:** `doc_type`, `source_category`, `journal`, `year` — пишутся
  в узел `:Document` и используются гео-фильтром и группировкой литобзора.

### Загрузка корпуса

```
1. /admin → «Загрузить корпус» (POST /api/v1/admin/load)
2. Дождаться stage: done  (в stats — documents, by_type, archives, chunks)
3. (опц.) Фаза 2 — извлечение структуры из документов:
   POST /api/v1/admin/extract {scope:"all"}  → NER :MENTIONS + LLM :Experiment/:Conclusion
   (без Ollama LLM-шаг пропускается, остаётся словарный NER)
4. Задавайте вопросы на главной
```

### Демо-пользователи

Пароль у всех: **`demo123`**

| Логин | Роль | Права |
|-------|------|-------|
| `researcher` | Исследователь | Q&A, эксплорер, пробелы |
| `analyst` | Аналитик | + сравнение, экспорт, история |
| `manager` | Руководитель | + метрики, аудит-лог, дашборды |
| `admin` | Администратор | + загрузка корпуса, правки графа |

---

## 🧭 Разделы UI

| Путь | Что это |
|------|---------|
| `/` | Главная с вопросом · гео-чипы · формат ответа (обычный / литобзор / сравнение) |
| `/answer/[id]` | Стрим ответа + подграф + источники · **экспорт MD / JSON-LD / PDF** |
| `/gaps` | Пробелы · тепловая карта покрытия + Link Prediction |
| `/timeline` | История значений свойства для материала во времени |
| `/compare` | Сравнение двух материалов или режимов по всем свойствам |
| `/explorer/[type]/[code]` | Эго-сеть сущности |
| `/admin` | Ingest корпуса, метрики экономии, лог auto-enrichment |
| `/login` | Вход (JWT) |

---

## 🔌 API

**Публичные:**
- `POST /api/v1/ask` — SSE-стрим Q&A с `geo_filter`, `intent_hint`
- `POST /api/v1/compare` — сравнение двух опций
- `GET /api/v1/gaps/data` + `POST /api/v1/gaps/hypothesis` — пробелы
- `GET /api/v1/timeline` — история
- `GET /api/v1/explorer/{type}/{code}` — эго-сеть
- `POST /api/v1/explain/edge` — LLM-объяснение ребра
- `POST /api/v1/export/{markdown|jsonld|pdf}` — экспорт
- `GET /api/v1/history/conclusion/{id}` — версии вывода
- `POST /api/v1/auth/login` · `GET /api/v1/auth/me` · `GET /api/v1/auth/audit`
- `POST /api/v1/admin/load` · `POST /api/v1/admin/enrich` · `POST /api/v1/admin/extract` · `GET /api/v1/admin/stats` · метрики
- `POST /api/v1/search/nl2cypher` — опциональный NL→Cypher (read-only, off по умолчанию)

Swagger: <http://localhost:8000/docs>

**Производительность:** CAG-кэш ответов Q&A (TTL 7 дней) + SHA-256-дедуп чанков (одинаковый boilerplate журналов не эмбеддится повторно) + FTS-сидинг ретрива (Neo4j full-text) для лексического recall по кодам и точным терминам.

---

## 🧠 Онтология

**11 типов узлов:** Material · Property · Mode · ModeParam · Equipment · Experiment · Conclusion · Document · Author · Team · Tag  
**16 типов рёбер:** USED_MATERIAL / USED_MODE / USED_EQUIPMENT / MEASURED / HAS_PARAM / RESULTED_IN / CONDUCTED_BY / MEMBER_OF / DOCUMENTED_IN / MENTIONS / CITES / SIMILAR_TO / TAGGED_WITH / CONFIRMS / CONTRADICTS / **SUPERSEDED_BY**

Полное описание → `АРХИТЕКТУРА_НОРНИКЕЛЬ.md` (в родительской папке).

---

## 📊 Метрики экономии

| Задача | Модель | Латентность | VRAM |
|--------|--------|-------------|------|
| Синтез ответа | Qwen 2.5 14B Q5 | ~40 ток/с · 1-й токен < 2 c | ~10 ГБ |
| NER / JSON-парсинг | Qwen 2.5 3B Q5 | < 200 мс на чанк | ~2 ГБ |
| Эмбеддинги 100 чанков | BGE-M3 | ~3 с | shared |
| Топологический анализ 1000 узлов | Neo4j GDS | < 500 мс | — |

Все метрики видны в `/admin`.

---

## 🎁 Deliverables хакатона

- ✅ **Автономный Прототип** (Docker Compose)
- ✅ **Интерфейсы** (Next.js + Nornickel-стиль)
- ✅ **Описание функционала** (этот README + `АРХИТЕКТУРА_НОРНИКЕЛЬ.md`)
- ✅ **Исходный код** (`backend/`, `frontend/`)
- ✅ **Минимальный дизайн** (Tailwind + фирменная палитра)
- ✅ **MIT License** (разрешена Положением §7.5)

---

## 🐛 Troubleshooting

**Ollama недоступен.** Запустите `ollama serve` на хосте — backend обращается через `host.docker.internal:11434`.

**Первый ответ медленный (30–60 с).** Прогрев Qwen в VRAM + fastembed качает BGE-M3.

**Q&A ничего не находит.** Проверьте `/admin` → `stage: done`. Убедитесь, что коды в вопросе есть в словарях.

**Хочу качество лучше.** В `.env`: `PREMIUM_MODE=true` → Qwen 32B (нужно ≥24 ГБ VRAM).

---

## 📄 Лицензия

**MIT.** Модели: Qwen 2.5 (Apache 2.0), BGE-M3 (MIT).
