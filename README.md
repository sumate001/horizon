# Horizon — News Intelligence Pipeline

ระบบเฝ้าระวังข่าวอัตโนมัติ 9 ขั้น: ingestion → fake-news gating → classification →
deduplication → event clustering → weak signal detection → driving force scoring →
trend scoring → scenario reasoning

Horizon เป็นระบบ**หลัก** (radar) ส่วน [OSINT//DESK](../osint-intelligence) คือ**ขั้นที่ 10** —
การสืบสวนเชิงลึกโดยนักวิเคราะห์ Horizon ส่ง signal เข้าไปทาง webhook และรับ verdict กลับมา
ทั้งสองระบบเป็น Docker stack แยกกันคนละชุด คุยกันผ่าน HTTP เท่านั้น ถ้าฝั่งใดล่ม อีกฝั่งทำงานต่อได้ปกติ

**สถานะ: Phase 1 (Skeleton + Ingestion)** — poller, worker, dedup 2 ชั้น, API, schema ครบทุกตาราง

---

## เริ่มใช้งาน

ต้องมี Docker 24+ พร้อม compose plugin และ Ollama ที่มี `qwen3:8b` + `bge-m3` โหลดอยู่แล้ว

```bash
cp .env.example .env
nano .env          # ตั้ง POSTGRES_PASSWORD, HORIZON_API_KEY, ตรวจ OLLAMA_BASE_URL
make up            # build + start: postgres, redis, qdrant, migrate, api, poller, worker
make logs          # ดู log สด
```

เมื่อขึ้นแล้ว:

| ปลายทาง | URL |
|---|---|
| API docs | http://localhost:8300/docs |
| Health | http://localhost:8300/health |
| Metrics (Prometheus) | http://localhost:8300/metrics |
| สถิติ ingestion | http://localhost:8300/api/v1/stats |
| เหตุการณ์ล่าสุด | http://localhost:8300/api/v1/events |

`make up` จะ migrate schema และ seed แหล่งข่าวไทยเริ่มต้น 12 แหล่ง + driving force PESTEL 6 ตัวให้อัตโนมัติ
poller เริ่มดึงทันทีโดยไม่รอครบ 15 นาที

> **ข้อควรระวังเรื่อง Ollama:** เซิร์ฟเวอร์ Ollama เป็นเครื่องที่ใช้ร่วมกัน (A5000, no-swap VRAM policy)
> ใช้ได้เฉพาะ `qwen3:8b` และ `bge-m3` ที่โหลดค้างอยู่แล้วเท่านั้น **ห้าม pull หรือโหลดโมเดลอื่น**

---

## พอร์ต

Horizon เลี่ยงพอร์ตของ OSINT//DESK ไว้แล้ว รันพร้อมกันบนเครื่องเดียวได้

| | Horizon | OSINT//DESK |
|---|---|---|
| compose project | `horizon` | `osintdesk` |
| network | `horizon_net` | (default ของ project) |
| API | 8300 | 8000 |
| UI | 8301 | 80 / 443 |
| Postgres | 5433 | ไม่ expose |
| Redis | 6380 | ไม่ expose |
| Qdrant | 6343 | — |

---

## สถาปัตยกรรม — สามจังหวะ

```
RHYTHM 1 — STREAMING (ทุก 15 นาที)          ← Phase 1 ✅
  poller ──> Redis queue ──> worker
    gate (credibility) → LLM extract+classify → dedup L1 (MinHash) → dedup L2 (bge-m3 + Qdrant)
    → PostgreSQL + Qdrant

RHYTHM 2 — BATCH (ทุก 3 ชม.)                ← Phase 2
  clustering (HDBSCAN) / trend scoring / weak signal detection → publish `horizon:signals`

RHYTHM 3 — EVENT-DRIVEN (subscriber)         ← Phase 3
  reasoner: driving force (PESTEL+AHP) → scenario (RAG) → Telegram/LINE → webhook ไป OSINT//DESK
```

### Dedup ทำงานยังไง

| ชั้น | วิธี | เกณฑ์ | ผลลัพธ์ |
|---|---|---|---|
| L1 | MinHash 128 perms บน character 5-gram ของ title+body | Jaccard ≥ `DEDUP_MINHASH_T` (0.85) | ซ้ำ — เพิ่ม `source_count`, ยก `credibility_weight` เป็นค่าสูงสุด |
| L2 | bge-m3 embedding ของ title+summary ค้นใน Qdrant ย้อนหลัง 7 วัน | cosine ≥ `DEDUP_COSINE_T` (0.88) | ซ้ำ **หรือ** update |

L1 จับข่าวที่ถูก syndicate มาแบบข้อความเดียวกัน (ต่างแค่พาดหัว) ส่วนข่าวที่ถูกเรียบเรียงใหม่
Jaccard จะตกต่ำกว่าเกณฑ์และตกไปให้ L2 ตัดสินด้วยความหมายแทน

**update vs duplicate:** ถ้า cosine ผ่านเกณฑ์แต่ `event_time` เปลี่ยน หรือ **ตัวเลขใน summary เปลี่ยน**
(รองรับทั้งเลขอารบิกและเลขไทย) จะถือเป็น *update* — เพิ่ม `source_count`, เขียน summary ใหม่,
และ append ประวัติลง `events.event_updates` (JSONB) เพื่อ audit

ภาษาไทยไม่มีการเว้นวรรคระหว่างคำ shingle จึงใช้ character n-gram ไม่ใช่ word n-gram

---

## คำสั่งที่ใช้บ่อย

```bash
make up          # build + start ทุก service
make logs        # log สดของ api, poller, worker
make ps          # สถานะ container
make down        # หยุด
make migrate     # รัน alembic upgrade head + seed
make test        # pytest ใน container
make test-unit   # pytest บนเครื่อง (ไม่ต้องมี DB)
make psql        # เข้า psql
```

รันเทสต์บนเครื่องโดยตรง:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

---

## จัดการแหล่งข่าว

```bash
# ดูทั้งหมด
curl -s localhost:8300/api/v1/sources | jq

# เพิ่ม (ต้องใส่ key ถ้าตั้ง HORIZON_API_KEY ไว้)
curl -X POST localhost:8300/api/v1/sources \
  -H "X-API-Key: $HORIZON_API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"ชื่อแหล่งข่าว","url":"https://example.com/feed","type":"rss","credibility_weight":0.8}'

# ปรับ credibility หรือปิดการใช้งาน
curl -X PATCH localhost:8300/api/v1/sources/<id> \
  -H "X-API-Key: $HORIZON_API_KEY" -H 'Content-Type: application/json' \
  -d '{"credibility_weight":0.6,"active":false}'
```

แหล่งข่าวที่ `credibility_weight < GATE_MIN_CREDIBILITY` (0.2) จะถูกเก็บเป็น
`raw_articles.status='dropped_lowcred'` และไม่กลายเป็น event

ค่า credibility ที่ seed ไว้เป็นเพียงค่าตั้งต้น — ควรให้นักวิเคราะห์ปรับตามการใช้งานจริง

---

## โครงสร้างโค้ด

```
horizon/
├── config.py            # ทุก threshold/weight อ่านจาก env — ห้าม hardcode
├── models.py            # SQLAlchemy 2.0 — ครบ 11 ตารางตั้งแต่ migration แรก
├── db.py                # async engine + session_scope()
├── queue.py             # Redis list: poller LPUSH → worker BRPOP
├── seed.py              # PESTEL 6 ตัว + แหล่งข่าวไทยเริ่มต้น (idempotent)
├── metrics.py           # Prometheus counters/histograms
├── llm/                 # Ollama client (chat JSON + embeddings) + prompt ภาษาไทย
├── pipeline/
│   ├── gate.py          # GateStrategy protocol — จุดต่อขยายสำหรับ WangchanBERTa
│   ├── extract.py       # step 1+3 — LLM เดียวจบ + repair prompt เมื่อ JSON พัง
│   ├── dedup.py         # step 4 — ตัดสินอย่างเดียว ไม่เขียน DB (เทสต์ได้โดยไม่ต้องมี DB)
│   └── vectors.py       # Qdrant wrapper
├── sources/             # rss / searxng fetcher + full-text extraction
└── services/            # poller / worker / api
contracts/               # JSON Schema ที่ใช้ร่วมกับ OSINT//DESK — source of truth
```

`pipeline/dedup.py` ตัดสินใจอย่างเดียว การเขียน DB ทั้งหมดอยู่ใน `services/worker.py`
ทำให้ทดสอบ logic dedup ได้โดยไม่ต้องยก Postgres หรือ Qdrant

---

## แผนงานถัดไป

| Phase | ขอบเขต | สถานะ |
|---|---|---|
| 1 | Skeleton + ingestion + dedup | ✅ |
| 2 | Batch: clustering, trend scoring, weak signals, pub/sub | ⬜ |
| 3 | Reasoner: driving force (AHP), scenario, Telegram/LINE | ⬜ |
| 4 | Integration: outbound webhook + inbound verdict endpoint | ⬜ |
| 5 | Dashboard 4 หน้า (Trends, Weak Signals, Scenarios, Sources) | ⬜ |
| 6 | Hardening: metrics ครบทุก service, health checks, structured logs | ⬜ |

รายละเอียดสเปคทั้งหมดอยู่ใน [CLAUDE.md](CLAUDE.md)
