# CLAUDE.md — Horizon: News Intelligence Pipeline

## Project Overview

Horizon is a **standalone** automated news intelligence system implementing a 9-step pipeline: ingestion → fake-news gating → classification → deduplication → event clustering → weak signal detection → driving force scoring → trend scoring → scenario reasoning.

It is **independent from OSINT//DESK** (separate repo, separate deployment) but integrates with it via two HTTP webhooks (outbound signal delivery, inbound verdict feedback). If OSINT//DESK is unreachable, Horizon must continue operating normally — the integration is fire-and-forget with retry, never blocking.

- Language: Python 3.11+ (services), React + Vite + TailwindCSS (dashboard)
- Deployment: Docker Compose on Ubuntu (Proxmox VM)
- LLM inference: shared Ollama server at `http://100.94.37.18:11434` (NVIDIA A5000, Tailscale). **Do NOT deploy a new Ollama.** Respect the no-swap VRAM policy: use only models already resident (`qwen3:8b`, `bge-m3` via Ollama embeddings API). Do not pull or load other models.
- All timestamps stored UTC; display timezone Asia/Bangkok.
- Primary content language is Thai; prompts must handle Thai + mixed Thai/English text.

## Architecture — Three Rhythms

```
RHYTHM 1 — STREAMING (every 15 min)
  horizon-poller ──> Redis queue ──> horizon-worker
    poller: RSS/SearXNG fetch, source registry lookup
    worker: fake-news gate (phase 1: credibility weight)
            → LLM extraction + classification (single call)
            → dedup L1 (MinHash) → dedup L2 (bge-m3 + Qdrant)
            → write to PostgreSQL + Qdrant

RHYTHM 2 — BATCH (cron, every 3 hours, configurable)
  horizon-batch:
    job A: event clustering (HDBSCAN, temporal)
    job B: trend scoring (frequency/velocity/acceleration, EWMA, Z-score)
    job C: weak signal detection (novelty + Isolation Forest + burst) — separate job, runs after A
    then: threshold gate evaluation → publish to Redis pub/sub channel `horizon:signals`

RHYTHM 3 — EVENT-DRIVEN (subscriber, idle until woken)
  horizon-reasoner: subscribes `horizon:signals`
    → driving force scoring (PESTEL pairwise + AHP)
    → scenario reasoning (RAG from event DB)
    → alert dispatch (Telegram + LINE Notify)
    → outbound webhook to OSINT//DESK (see Integration Contract)
```

## Services (docker-compose)

| Service | Role | Notes |
|---|---|---|
| `horizon-poller` | Fetch feeds every 15 min | APScheduler; sources in `sources` table |
| `horizon-worker` | Consume queue, extract, dedup, persist | Concurrency 2; backpressure via queue depth |
| `horizon-batch` | Clustering + scoring cron | Ofelia or supercronic container |
| `horizon-reasoner` | Driving force + scenario + alerts + webhook | Redis pub/sub subscriber |
| `horizon-api` | FastAPI: dashboard API + inbound verdict endpoint | Port 8300 |
| `horizon-ui` | React dashboard | Served via nginx, port 8301 |
| `postgres` | Primary store | Dedicated instance for this repo |
| `redis` | Queue + pub/sub | |
| `qdrant` | Vector store | Dedicated instance; collection `horizon_events` |

## Database Schema (PostgreSQL)

Implement these tables (add indexes on all FK and time columns):

- `sources(id, name, url, type[rss|searxng|watchlist_hook], credibility_weight FLOAT 0..1, active, created_at)`
- `raw_articles(id, source_id FK, url UNIQUE, title, body, fetched_at, lang, status[queued|processed|dropped_duplicate|dropped_lowcred|failed])`
- `events(id, raw_article_id FK, actors JSONB, action TEXT, location TEXT, event_time TIMESTAMPTZ NULL, categories TEXT[], summary TEXT, extraction_confidence FLOAT, incomplete BOOLEAN, source_count INT DEFAULT 1, credibility_weight FLOAT, embedding_id UUID, cluster_id FK NULL, created_at)`
- `clusters(id, label TEXT NULL, first_seen, last_seen, event_count, status[active|dormant], centroid_id UUID)`
- `scores(id, cluster_id FK, window_start, window_end, frequency FLOAT, velocity FLOAT, acceleration FLOAT, z_frequency FLOAT, z_velocity FLOAT, z_acceleration FLOAT, trend_score FLOAT, computed_at)`
- `weak_signals(id, event_id FK NULL, cluster_id FK NULL, novelty_score FLOAT, isolation_score FLOAT, burst_score FLOAT, combined_score FLOAT, status[candidate|dispatched|verified_true|verified_false|expired], created_at)`
- `driving_forces(id, name, dimension[P|E|S|T|E2|L], definition TEXT, weight FLOAT, active)` — seed with 6 PESTEL rows; definitions editable via API
- `force_assessments(id, cluster_id FK, force_id FK, impact FLOAT, uncertainty FLOAT, ahp_rank INT, assessed_at)`
- `scenarios(id, cluster_id FK, best_case TEXT, worst_case TEXT, likely_case TEXT, indicators JSONB, source_event_ids UUID[], model TEXT, created_at)`
- `dispatches(id, signal_type[weak_signal|trend_breakout], ref_id UUID, channels TEXT[], osint_desk_status[pending|delivered|failed|disabled], osint_desk_signal_id TEXT NULL, payload JSONB, created_at)`
- `verdicts(id, dispatch_id FK, verdict[true_signal|false_signal|inconclusive], analyst_note TEXT, received_at)` — populated by inbound webhook

## Pipeline Implementation Details

### Step 1+3 — Extraction + Classification (single LLM call)
- Model: `qwen3:8b` via Ollama `/api/chat`, `temperature: 0`, `format: json`.
- Prompt requirements: output MUST match this schema exactly; closed category list: `["การเมือง","เศรษฐกิจ","ความมั่นคง","เทคโนโลยี","สังคม","สิ่งแวดล้อม","ต่างประเทศ","พลังงาน","บันเทิง/กีฬา"]` (multi-label, 1–3 labels); normalize entity names (Thai transliteration ↔ English: keep both in `actors` as canonical string, e.g. `"Fed (เฟด)"`); ISO-8601 for `time`, null if not stated — never guess dates.
```json
{
  "actors": ["string"],
  "action": "string",
  "location": "string|null",
  "time": "ISO-8601|null",
  "categories": ["string"],
  "summary": "string (<= 280 chars, Thai)",
  "confidence": 0.0
}
```
- If JSON parse fails: retry once with a repair prompt; on second failure mark `raw_articles.status='failed'` and continue. Never crash the worker on a single article.
- Mark `events.incomplete = true` when `actors` empty or `action` missing; incomplete events are stored but excluded from clustering and scoring.

### Step 2 — Fake News Gate (Phase 1 only in this build)
- No ML model. Apply `sources.credibility_weight` to every event.
- Articles from sources with weight < 0.2 are stored with `status='dropped_lowcred'` and do not become events.
- Leave a clearly marked extension point (`gate.py` with a `GateStrategy` interface) for the future WangchanBERTa classifier. Do not implement the classifier now.

### Step 4 — Deduplication (two layers, ordered cheap → expensive)
- L1: MinHash over title+body shingles (datasketch, 128 perms, Jaccard threshold 0.85). Match → increment `source_count` on the existing event, update its `credibility_weight` to max(existing, new), stop.
- L2: embed title+summary with `bge-m3` (Ollama embeddings endpoint), search `horizon_events` collection in Qdrant, cosine ≥ 0.88 within a 7-day window → duplicate (same handling as L1). Below threshold → insert new event + upsert vector.
- **Update vs duplicate:** if cosine ≥ 0.88 but extracted fields differ materially (different `time`, or numeric values in summary changed), link as update: increment `source_count`, refresh `summary`, append to an `event_updates` JSONB audit column.
- Thresholds must be env-configurable (`DEDUP_MINHASH_T`, `DEDUP_COSINE_T`).

### Step 5 — Event Clustering
- HDBSCAN over event embeddings, `min_cluster_size` env-configurable (default 4), metric: cosine distance on embeddings concatenated with a scaled temporal feature (days since epoch × `TEMPORAL_WEIGHT`, default 0.15). Events further apart than 14 days should rarely co-cluster.
- Runs over a rolling 30-day window of complete events. Cluster assignments are stable-labeled: match new clusters to previous run by centroid similarity ≥ 0.9 to preserve `cluster_id` across runs; unmatched clusters get new IDs.
- Noise points (label −1) keep `cluster_id = NULL` — they are input for step 6.

### Step 6 — Weak Signal Detection (separate batch job)
- Candidates: noise events + clusters with `event_count <= 5`.
- `novelty_score`: 1 − max cosine similarity to any cluster centroid.
- `isolation_score`: Isolation Forest (scikit-learn) over features `[reports_per_day, distinct_sources, mean_credibility, category_rarity]`.
- `burst_score`: Kleinberg burst detection on the candidate's daily counts (use `burst_detection` lib or implement 2-state automaton).
- `combined_score = (0.4·novelty + 0.3·isolation + 0.3·burst) × mean_credibility`. Weights env-configurable.
- Candidates with `combined_score ≥ WEAK_SIGNAL_T` (default 0.65) → insert `weak_signals(status='candidate')` and publish to `horizon:signals`.

### Step 7 — Driving Force Scoring (in reasoner, gated)
- Triggered only for signals/clusters arriving on `horizon:signals`.
- For each active driving force: LLM pairwise comparisons between the triggered cluster and the current top-5 clusters (prompt: "which impacts <force definition> more, A or B? answer A/B only"), `temperature 0`.
- Build pairwise matrix → AHP principal eigenvector → `impact` (normalized 0–1). Ask one direct LLM question for `uncertainty` (low/medium/high → 0.2/0.5/0.8).
- Persist to `force_assessments`.

### Step 8 — Trend Scoring
- Pure statistics. Per active cluster per 6h window: `frequency = Σ(source_count × credibility_weight)` of new events; `velocity = Δfrequency`; `acceleration = Δvelocity`. Smooth each with EWMA (span 6 windows). Z-scores against that cluster's own trailing 28-day history.
- `trend_score = 0.3·z_freq + 0.4·z_vel + 0.3·z_accel` (weights env-configurable).
- `trend_score ≥ TREND_BREAKOUT_T` (default 2.5) → publish `trend_breakout` to `horizon:signals`.
- Until 14 days of history exist for a cluster, mark scores `provisional: true` in API responses and suppress breakout publishing.

### Step 9 — Scenario Reasoning
- RAG context: all events in cluster (summaries, capped at 50 most recent), force_assessments, trend history (last 10 windows), top-3 related clusters by centroid similarity.
- Model: `qwen3:8b`. Output JSON: `{best_case, worst_case, likely_case, indicators: [{description, watch_type}]}` — Thai language.
- Every scenario stored with `source_event_ids` for auditability. UI must display "ฉากทัศน์เป็นความเป็นไปได้ที่มีเงื่อนไข ไม่ใช่คำพยากรณ์ — ต้องผ่านการกลั่นกรองของนักวิเคราะห์".

### Alert Dispatch
- Channels: Telegram bot + LINE Notify (tokens via env). Message: signal type, cluster label, score, top summary, dashboard link.
- Also POST to OSINT//DESK (next section). Record all in `dispatches`.

## Integration Contract with OSINT//DESK

**This schema is the shared contract. It must match the OSINT//DESK side byte-for-byte. Do not alter field names without updating both repos.**

### Outbound: Horizon → OSINT//DESK (signal delivery)

`POST {OSINT_DESK_BASE_URL}/api/v1/signals/inbound`
Headers: `X-API-Key: {OSINT_DESK_API_KEY}`, `Content-Type: application/json`

```json
{
  "signal_id": "uuid (dispatches.id)",
  "signal_type": "weak_signal | trend_breakout",
  "title": "string (cluster label or event summary, Thai)",
  "combined_score": 0.0,
  "trend_score": 0.0,
  "categories": ["string"],
  "summary": "string (Thai)",
  "top_events": [
    {"summary": "string", "url": "string", "source_name": "string",
     "credibility_weight": 0.0, "event_time": "ISO-8601|null"}
  ],
  "force_assessments": [
    {"force": "string", "impact": 0.0, "uncertainty": 0.0}
  ],
  "scenario_id": "uuid|null",
  "created_at": "ISO-8601"
}
```
- `top_events` capped at 10, ordered by credibility_weight desc.
- Expected response: `202 {"osint_signal_id": "string"}` → store in `dispatches.osint_desk_signal_id`, status `delivered`.
- Failure handling: retry with exponential backoff (30s, 2m, 10m, 1h, then give up → status `failed`). Failures must never block alerting or any pipeline stage. If `OSINT_DESK_BASE_URL` unset → status `disabled`, skip silently.

### Inbound: OSINT//DESK → Horizon (verdict feedback)

Horizon exposes: `POST /api/v1/verdicts`
Headers: `X-API-Key: {HORIZON_API_KEY}`

```json
{
  "signal_id": "uuid (the original dispatches.id)",
  "osint_signal_id": "string",
  "verdict": "true_signal | false_signal | inconclusive",
  "analyst_note": "string|null",
  "closed_at": "ISO-8601"
}
```
- Response `200 {"ok": true}`. Unknown `signal_id` → `404`. Bad key → `401`.
- On receipt: insert `verdicts`, update `weak_signals.status` to `verified_true/verified_false`. These labels are the feedback corpus for future threshold tuning — expose `GET /api/v1/verdicts/export` (JSONL) for offline analysis.

## Dashboard (horizon-ui)

Single-page React app, pages:
1. **Trends** — ranked clusters by trend_score, sparkline of score history, category filter, provisional badge.
2. **Weak Signals** — candidate list with three component scores, status chips, "dispatched to OSINT//DESK" indicator + verdict when returned.
3. **Scenarios** — per-cluster scenario cards with indicators and source event list (audit view).
4. **Sources** — CRUD for source registry incl. credibility_weight editing (auth required).
Keep styling minimal (Tailwind); dark theme.

## Configuration (.env.example — create it)

```
OLLAMA_BASE_URL=http://100.94.37.18:11434
EXTRACT_MODEL=qwen3:8b
EMBED_MODEL=bge-m3
DEDUP_MINHASH_T=0.85
DEDUP_COSINE_T=0.88
MIN_CLUSTER_SIZE=4
TEMPORAL_WEIGHT=0.15
WEAK_SIGNAL_T=0.65
TREND_BREAKOUT_T=2.5
OSINT_DESK_BASE_URL=
OSINT_DESK_API_KEY=
HORIZON_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
LINE_NOTIFY_TOKEN=
```

## Build Phases (implement in this order)

1. **Phase 1 — Skeleton + Ingestion**: compose file, Postgres schema + migrations (alembic), poller, queue, worker with extraction + both dedup layers. Deliverable: articles flow in and become deduplicated events. *Deploy this phase to production immediately so baseline data accumulates while later phases are built.*
2. **Phase 2 — Batch analytics**: clustering job, trend scoring, weak signal job, threshold gate + pub/sub publishing.
3. **Phase 3 — Reasoner**: driving force scoring, scenario generation, Telegram/LINE alerts.
4. **Phase 4 — Integration**: outbound webhook client with retry, inbound verdict endpoint, dispatches/verdicts tables wiring.
5. **Phase 5 — Dashboard**: all four UI pages.
6. **Phase 6 — Hardening**: Prometheus metrics endpoint on each service (queue depth, extraction latency, dedup hit rate, LLM failure rate), structured JSON logging, health checks in compose.

## Testing Requirements

- Unit tests for: dedup logic (both layers, update-vs-duplicate), trend score math (fixed fixtures → exact expected values), AHP computation, webhook retry/backoff.
- Integration test: seed 20 fixture articles (Thai, incl. 5 duplicates + 2 near-duplicates) → assert event count, source_counts, cluster formation.
- Contract test: validate outbound payload and inbound verdict against JSON Schema files in `contracts/` — commit the schema files; they are the source of truth shared with OSINT//DESK.

## Coding Conventions

- FastAPI + Pydantic v2 models mirroring the contract schemas.
- All thresholds/weights read from env, never hardcoded.
- Every LLM call wrapped with timeout (60s), retry-once, and failure metrics. A single article/cluster failure must never halt a loop.
- Comments and commit messages in English; user-facing strings (alerts, UI) in Thai.
