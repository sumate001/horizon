import { useState } from "react";

import { api, apiKey, usePoll } from "../api";
import type { EntityRow } from "../api";
import { ApiKeyBar, Empty, ErrorBox, Stat, fmtAgo } from "../components/ui";

/**
 * The entity store, and the queue of things nobody has confirmed yet.
 *
 * Merging two people wrongly is the failure that matters here: it becomes false
 * history in an OSINT//DESK case file, and the analyst reading it has no way to
 * tell. So every row shows the surface forms the articles actually used — the
 * evidence for the merge, not just its conclusion.
 */

const TYPE_LABEL: Record<string, string> = {
  person: "บุคคล",
  org: "องค์กร",
  place: "สถานที่",
  team: "ทีม",
  generic: "คำทั่วไป",
  unknown: "ยังไม่ระบุ",
};

const TYPE_TONE: Record<string, string> = {
  person: "bg-cyan-500/15 text-cyan-300",
  org: "bg-violet-500/15 text-violet-300",
  place: "bg-emerald-500/15 text-emerald-300",
  team: "bg-amber-500/15 text-amber-300",
  generic: "bg-slate-500/15 text-slate-500",
  unknown: "bg-slate-500/15 text-slate-400",
};

const TABS: { key: string; label: string }[] = [
  { key: "needs_review", label: "รอตรวจ" },
  { key: "auto", label: "ระบบตัดสินเอง" },
  { key: "confirmed", label: "ยืนยันแล้ว" },
  { key: "rejected", label: "ตีกลับ" },
];

function Row({ entity, onDone, canDecide }: { entity: EntityRow; onDone: () => void; canDecide: boolean }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pending = entity.review_status === "needs_review";

  async function decide(decision: "confirmed" | "rejected") {
    setBusy(decision);
    setError(null);
    try {
      await api.reviewEntity(entity.id, { decision });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="card px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-sm text-slate-200">{entity.canonical_name}</span>
        <span className={`chip ${TYPE_TONE[entity.entity_type]}`}>
          {TYPE_LABEL[entity.entity_type] ?? entity.entity_type}
        </span>
        {entity.qid ? (
          <a
            href={`https://www.wikidata.org/wiki/${entity.qid}`}
            target="_blank"
            rel="noreferrer"
            title={entity.qid_reason ?? undefined}
            className="chip bg-blue-500/15 font-mono text-blue-300 hover:bg-blue-500/25"
          >
            {entity.qid}
          </a>
        ) : (
          entity.qid_status === "no_match" && (
            <span className="chip bg-slate-500/10 text-[11px] text-slate-600">
              ไม่มีใน Wikidata
            </span>
          )
        )}
        <span className="ml-auto font-mono text-[11px] text-slate-500">
          {entity.mention_count} ครั้ง · {fmtAgo(entity.last_seen)}
        </span>
      </div>

      {entity.risk && pending && (
        <p className="mt-1.5 text-[11px] text-amber-300/90">ทำไมต้องตรวจ: {entity.risk}</p>
      )}

      {entity.surface_forms.length > 0 && (
        <div className="mt-2">
          <p className="text-[10px] uppercase tracking-wide text-slate-600">
            ข่าวเขียนไว้แบบนี้ ({entity.surface_forms.length} แบบ)
          </p>
          <div className="mt-1 flex flex-wrap gap-1">
            {entity.surface_forms.map((form) => (
              <span key={form} className="chip bg-ink-600 text-[11px] text-slate-400">
                {form}
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="mt-2 flex items-center gap-3 text-[11px] text-slate-600">
        <span>
          ความมั่นใจ{" "}
          <span className={entity.confidence < 0.75 ? "text-amber-300" : "text-emerald-300"}>
            {entity.confidence.toFixed(2)}
          </span>
        </span>
        <span>ตัดสินโดย {entity.decided_by === "llm" ? "AI" : "กฎ"}</span>
        {pending && (
          <span className="ml-auto flex gap-2">
            <button
              onClick={() => decide("confirmed")}
              disabled={!!busy || !canDecide}
              className="rounded bg-emerald-600/80 px-3 py-1 text-white hover:bg-emerald-600 disabled:opacity-40"
            >
              {busy === "confirmed" ? "…" : "ถูกต้อง"}
            </button>
            <button
              onClick={() => decide("rejected")}
              disabled={!!busy || !canDecide}
              className="rounded bg-red-600/80 px-3 py-1 text-white hover:bg-red-600 disabled:opacity-40"
            >
              {busy === "rejected" ? "…" : "รวมผิด"}
            </button>
          </span>
        )}
      </div>
      {error && <p className="mt-2 text-[11px] text-red-300">{error}</p>}
    </div>
  );
}

export default function Entities() {
  const [tab, setTab] = useState("needs_review");
  // apiKey() is read during render, so a save has to be announced or the
  // buttons stay disabled until the next poll — which reads as the save not
  // having worked.
  const [hasKey, setHasKey] = useState(!!apiKey());
  const counts = usePoll(api.entityCounts, 30000);
  const { data, error, loading, reload } = usePoll(() => api.entities(tab), 0, [tab]);

  const review = counts.data?.review ?? {};
  const wikidata = counts.data?.wikidata ?? {};

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-sm font-semibold text-slate-300">ทะเบียนตัวตน</h1>
        <p className="mt-1 text-[11px] leading-relaxed text-slate-500">
          ชื่อที่สกัดจากข่าวถูกรวมเป็น "สิ่งเดียวกัน" แล้วให้เลขประจำตัว เพื่อให้ฝั่ง
          OSINT//DESK ตอบได้ว่าเคยเจอคนนี้ในคดีไหนมาก่อน — การรวมผิดจะกลายเป็นประวัติปลอม
          ในแฟ้มคดี จึงต้องมีคนกดยืนยันเมื่อระบบไม่มั่นใจ
        </p>
      </div>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat
          label="รอตรวจ"
          value={review.needs_review ?? "—"}
          tone={(review.needs_review ?? 0) > 0 ? "text-amber-300" : undefined}
        />
        <Stat label="ระบบตัดสินเอง" value={review.auto ?? "—"} />
        <Stat label="ยืนยันแล้ว" value={review.confirmed ?? "—"} tone="text-emerald-300" />
        <Stat label="ทั้งหมด" value={review.ALL ?? "—"} />
      </section>

      {/* Coverage is the honest number here: measured on this corpus Wikidata
          knows 75% of the names that repeat and a third of the ones seen once,
          so "ยังไม่ได้ตรวจ" falling to zero is the goal, not "ผูกแล้ว" rising. */}
      <section className="card flex flex-wrap items-center gap-x-6 gap-y-1 px-5 py-3 text-[11px]">
        <span className="text-slate-500">การผูกกับ Wikidata</span>
        <span className="text-blue-300">
          ผูกแล้ว <span className="font-mono">{wikidata.linked ?? 0}</span>
        </span>
        <span className="text-slate-500">
          ไม่มีในคลัง <span className="font-mono">{wikidata.no_match ?? 0}</span>
        </span>
        <span className={wikidata.pending ? "text-amber-300" : "text-slate-600"}>
          ยังไม่ได้ตรวจ <span className="font-mono">{wikidata.pending ?? 0}</span>
        </span>
        {!!wikidata.unavailable && (
          <span className="text-red-300">
            ต่อ Wikidata ไม่ได้ <span className="font-mono">{wikidata.unavailable}</span>
          </span>
        )}
      </section>

      <div className="flex flex-wrap gap-1">
        {TABS.map((item) => (
          <button
            key={item.key}
            onClick={() => setTab(item.key)}
            className={`rounded px-3 py-1.5 text-sm transition-colors ${
              tab === item.key
                ? "bg-ink-600 text-slate-100"
                : "text-slate-400 hover:bg-ink-700 hover:text-slate-200"
            }`}
          >
            {item.label}
            {review[item.key] != null && (
              <span className="ml-1.5 font-mono text-[11px] text-slate-500">{review[item.key]}</span>
            )}
          </button>
        ))}
      </div>

      {error && <ErrorBox message={error} />}
      {!hasKey && tab === "needs_review" && (
        <ApiKeyBar
          note="ปุ่ม ถูกต้อง / รวมผิด ถูกล็อกอยู่ ใส่ HORIZON_API_KEY แล้วกดบันทึกเพื่อปลดล็อก"
          onSaved={() => setHasKey(true)}
        />
      )}

      {loading ? (
        <p className="text-sm text-slate-500">กำลังโหลด…</p>
      ) : !data?.length ? (
        <Empty
          title={tab === "needs_review" ? "ไม่มีอะไรค้างให้ตรวจ" : "ยังไม่มีข้อมูล"}
          hint={
            review.ALL
              ? undefined
              : "ทะเบียนจะเริ่มมีข้อมูลเมื่อเปิด ENTITY_RESOLUTION_ENABLED แล้วมีข่าวเข้ามารอบใหม่"
          }
        />
      ) : (
        <div className="space-y-2">
          {data.map((entity) => (
            <Row
              key={entity.id}
              entity={entity}
              canDecide={hasKey}
              onDone={() => {
                reload();
                counts.reload();
              }}
            />
          ))}
        </div>
      )}
    </div>
  );
}
