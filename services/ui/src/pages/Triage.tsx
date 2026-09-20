import { useState } from "react";

import { api, apiKey, usePoll } from "../api";
import type { TriageDistribution } from "../api";
import { ApiKeyBar, ErrorBox } from "../components/ui";

/**
 * Tuning the editorial formula against real data.
 *
 * The six dimensions a story is scored on are stored on the row, so trying a
 * different sensitivity coefficient costs a division rather than re-reading
 * every article. That makes this a place to decide with numbers instead of
 * arguing about them.
 */

const PRESETS = [0.1, 0.05, 0.03, 0.0];

const VERDICT_TONE: Record<string, string> = {
  PRIORITY: "text-red-300",
  FAST_TRACK: "text-amber-300",
  INVESTIGATE: "text-blue-300",
  PASS: "text-slate-400",
};

function Distribution({ dist, events }: { dist: unknown; events: number }) {
  const d = dist as TriageDistribution | undefined;
  if (!d?.verdicts) return null;
  const order = ["PRIORITY", "FAST_TRACK", "INVESTIGATE", "PASS"];
  return (
    <div className="space-y-1.5">
      {order.map((v) => {
        const n = d.verdicts[v] ?? 0;
        return (
          <div key={v} className="flex items-center gap-2 text-sm">
            <span className={`w-28 shrink-0 font-mono text-[12px] ${VERDICT_TONE[v]}`}>{v}</span>
            <span className="h-2 flex-1 overflow-hidden rounded bg-ink-600">
              <span
                className={`block h-full ${VERDICT_TONE[v].replace("text-", "bg-")}`}
                style={{ width: `${events ? (n / events) * 100 : 0}%` }}
              />
            </span>
            <span className="w-8 shrink-0 text-right font-mono text-[12px] text-slate-400">{n}</span>
          </div>
        );
      })}
      <p className="pt-1 text-[11px] text-slate-500">
        ค่าเฉลี่ย {d.mean_total} ·{" "}
        <span className={d.at_ceiling_pct > 5 ? "text-red-300" : "text-emerald-300"}>
          ชนเพดาน {d.at_ceiling}/{events} ({d.at_ceiling_pct}%)
        </span>
      </p>
    </div>
  );
}

export default function Triage() {
  const [k, setK] = useState<number | null>(null);
  const { data, error, loading } = usePoll(
    () => api.simulateTriage(k ?? undefined),
    0,
    [k],
  );
  const [applying, setApplying] = useState(false);
  const [hasKey, setHasKey] = useState(!!apiKey());
  const [applied, setApplied] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);

  async function apply() {
    setApplying(true);
    setApplyError(null);
    try {
      const result = await api.rescoreTriage();
      setApplied(`ปรับใหม่ ${result.events} เหตุการณ์ · เปลี่ยน ${result.changed}`);
    } catch (err) {
      setApplyError(err instanceof Error ? err.message : String(err));
    } finally {
      setApplying(false);
    }
  }

  if (error) return <ErrorBox message={error} />;
  if (loading || !data) return <p className="text-sm text-slate-500">กำลังโหลด…</p>;

  const current = data.settings?.current?.sensitivity_coefficient;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-sm font-semibold text-slate-300">ปรับสูตรให้คะแนนเชิงบรรณาธิการ</h1>
        <p className="mt-1 text-[11px] leading-relaxed text-slate-500">
          total = ค่าเฉลี่ย 6 มิติ × (1 + sensitivity × k) · ทดลองกับ {data.events} เหตุการณ์จริง
          ที่มีอยู่ ไม่ต้องเรียก LLM ซ้ำ เพราะคะแนนดิบเก็บไว้แล้ว
        </p>
      </div>

      <div className="card px-5 py-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <span className="text-[11px] uppercase tracking-wide text-slate-500">ลองค่า k</span>
          {PRESETS.map((preset) => (
            <button
              key={preset}
              onClick={() => setK(preset)}
              className={`rounded px-3 py-1 font-mono text-[12px] transition-colors ${
                (k ?? current) === preset
                  ? "bg-cyan-600/80 text-white"
                  : "bg-ink-700 text-slate-400 hover:bg-ink-600"
              }`}
            >
              {preset.toFixed(2)}
              {preset === current && <span className="ml-1 text-[10px] opacity-70">ใช้อยู่</span>}
            </button>
          ))}
        </div>

        <div className="grid gap-6 md:grid-cols-2">
          <div>
            <p className="mb-2 text-[11px] uppercase tracking-wide text-slate-500">
              ที่ใช้อยู่ (k = {current})
            </p>
            <Distribution dist={data.current} events={data.events} />
          </div>
          <div>
            <p className="mb-2 text-[11px] uppercase tracking-wide text-slate-500">
              ถ้าเปลี่ยนเป็น k = {data.settings?.proposed?.sensitivity_coefficient}
            </p>
            <Distribution dist={data.proposed} events={data.events} />
          </div>
        </div>
      </div>

      <div className="card px-5 py-4">
        <p className="text-sm text-slate-300">นำค่าที่ตั้งไว้ใน .env ไปใช้กับเหตุการณ์ที่มีอยู่</p>
        <p className="mt-1 text-[11px] leading-relaxed text-slate-500">
          verdict ถูกคำนวณตอนบันทึก การเปลี่ยน <code>TRIAGE_SENSITIVITY_COEFFICIENT</code> ใน
          .env จึงมีผลกับข่าวใหม่เท่านั้น จนกว่าจะกดปุ่มนี้ — ไม่มีการเรียก LLM
        </p>
        {applyError && (
          <p className="mt-2 text-[11px] text-red-300">
            {applyError.includes("401") ? "ต้องใส่ API key ในหน้าแหล่งข่าวก่อน" : applyError}
          </p>
        )}
        {applied && <p className="mt-2 text-[11px] text-emerald-300">{applied}</p>}
        <button
          onClick={apply}
          disabled={applying || !hasKey}
          className="mt-3 rounded-lg bg-cyan-600/80 px-4 py-2 text-sm text-white hover:bg-cyan-600 disabled:opacity-50"
        >
          {applying ? "กำลังคำนวณใหม่…" : "คำนวณคะแนนใหม่ทั้งหมด"}
        </button>
        {!hasKey && (
          <div className="mt-3">
            <ApiKeyBar
              note="ปุ่มคำนวณคะแนนใหม่ถูกล็อกอยู่ ใส่ HORIZON_API_KEY แล้วกดบันทึกเพื่อปลดล็อก"
              onSaved={() => setHasKey(true)}
            />
          </div>
        )}
      </div>
    </div>
  );
}
