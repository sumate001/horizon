import { useState } from "react";

import { api, apiKey, usePoll } from "../api";
import type { EntityRow, Mention } from "../api";
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

/**
 * The articles behind an entity, one row each, selectable.
 *
 * Without this the queue asked "are these the same thing?" while showing only
 * the names — the reviewer had to guess, and ถูกต้อง / รวมผิด were the only two
 * answers available for a mistake that is almost always partial: twelve of
 * fourteen articles right and two wrong. Ticking the two and splitting them off
 * is the decision the data actually supports.
 */
function Mentions({
  entity,
  canDecide,
  onDone,
}: {
  entity: EntityRow;
  canDecide: boolean;
  onDone: () => void;
}) {
  const { data, error, loading } = usePoll(() => api.entityMentions(entity.id), 0, [entity.id]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const mentions: Mention[] = data ?? [];
  // Splitting every mention would leave nothing behind and duplicate the
  // entity; the server refuses it, so the button says so first.
  const all = mentions.length > 0 && picked.size >= mentions.length;

  function toggle(id: string) {
    setPicked((was) => {
      const next = new Set(was);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }

  async function split() {
    setBusy(true);
    setFailure(null);
    try {
      await api.splitEntity(entity.id, [...picked]);
      setPicked(new Set());
      onDone();
    } catch (err) {
      setFailure(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <p className="mt-2 text-[11px] text-slate-500">กำลังโหลดข่าว…</p>;
  if (error) return <p className="mt-2 text-[11px] text-red-300">โหลดข่าวไม่สำเร็จ — {error}</p>;
  if (!mentions.length)
    return <p className="mt-2 text-[11px] text-slate-500">ไม่มีข่าวที่อ้างถึงตัวตนนี้</p>;

  return (
    <div className="mt-3 space-y-1.5 border-t border-ink-600 pt-3">
      <p className="text-[10px] uppercase tracking-wide text-slate-600">
        ข่าวที่อ้างถึง ({mentions.length}) — ติ๊กข่าวที่ไม่ใช่ตัวตนนี้
      </p>
      {mentions.map((m) => (
        <label
          key={m.id}
          className={`flex cursor-pointer gap-2 rounded px-2 py-1.5 text-[11px] ${
            picked.has(m.id) ? "bg-red-500/10" : "hover:bg-ink-600/50"
          }`}
        >
          <input
            type="checkbox"
            checked={picked.has(m.id)}
            onChange={() => toggle(m.id)}
            disabled={!canDecide || busy}
            className="mt-0.5 shrink-0"
          />
          <span className="min-w-0 flex-1">
            <span className="chip mr-1.5 bg-ink-600 text-[10px] text-slate-300">
              {m.surface_form}
            </span>
            <span className="text-slate-400">{m.summary || "(ข่าวนี้ไม่มีสรุป)"}</span>
            <span className="mt-0.5 block text-[10px] text-slate-600">
              {fmtAgo(m.occurred_at)}
              {m.article_url && (
                <>
                  {" · "}
                  <a
                    href={m.article_url}
                    target="_blank"
                    rel="noreferrer"
                    onClick={(e) => e.stopPropagation()}
                    className="text-cyan-500 hover:text-cyan-400"
                  >
                    เปิดข่าวต้นทาง
                  </a>
                </>
              )}
            </span>
          </span>
        </label>
      ))}

      {failure && <p className="text-[11px] text-red-300">{failure}</p>}

      {picked.size > 0 && (
        <div className="flex items-center gap-2 pt-1">
          <button
            onClick={split}
            disabled={!canDecide || busy || all}
            className="rounded bg-amber-600/80 px-3 py-1 text-[11px] text-white hover:bg-amber-600 disabled:opacity-40"
          >
            {busy ? "…" : `แยก ${picked.size} ข่าวนี้ออกไป`}
          </button>
          <span className="text-[10px] text-slate-600">
            {all
              ? "ติ๊กครบทุกข่าวแล้ว — แบบนี้ให้กด รวมผิด แทน"
              : "ข่าวที่ติ๊กจะย้ายไปเป็นตัวตนใหม่ รอตรวจต่อ"}
          </span>
        </div>
      )}
    </div>
  );
}


function Row({ entity, onDone, canDecide }: { entity: EntityRow; onDone: () => void; canDecide: boolean }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
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
          <div className="flex items-baseline gap-2">
            <p className="text-[10px] uppercase tracking-wide text-slate-600">
              ข่าวเขียนไว้แบบนี้ ({entity.surface_forms.length} แบบ)
            </p>
            {pending && (
              <button
                onClick={() => setOpen((was) => !was)}
                className="text-[11px] text-cyan-400 hover:text-cyan-300"
              >
                {open ? "ซ่อนข่าวที่อ้างถึง" : "ดูข่าวที่อ้างถึง"}
              </button>
            )}
          </div>
          <div className="mt-1 flex flex-wrap gap-1">
            {entity.surface_forms.map((form) => (
              <span key={form} className="chip bg-ink-600 text-[11px] text-slate-400">
                {form}
              </span>
            ))}
          </div>
        </div>
      )}

      {open && <Mentions entity={entity} canDecide={canDecide} onDone={onDone} />}

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
            {/* Named for what it does. It sets review_status='rejected', which
                stops *new* articles attaching — it does not take apart what is
                already merged. Splitting is the operation for that, and calling
                this one "รวมผิด" promised an unmerge it never performed. */}
            <button
              onClick={() => decide("rejected")}
              disabled={!!busy || !canDecide}
              title="ไม่ให้ข่าวใหม่มาเกาะตัวตนนี้อีก (ข่าวที่เกาะอยู่แล้วให้ใช้การแยกรายข่าว)"
              className="rounded bg-red-600/80 px-3 py-1 text-white hover:bg-red-600 disabled:opacity-40"
            >
              {busy === "rejected" ? "…" : "ทิ้งทั้งตัวตน"}
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
