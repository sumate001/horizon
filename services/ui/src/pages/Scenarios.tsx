import { api, usePoll } from "../api";
import { Empty, ErrorBox, fmtTime } from "../components/ui";

// Required by the spec — scenarios must never read as predictions.
const DISCLAIMER =
  "ฉากทัศน์เป็นความเป็นไปได้ที่มีเงื่อนไข ไม่ใช่คำพยากรณ์ — ต้องผ่านการกลั่นกรองของนักวิเคราะห์";

const CASES = [
  { key: "best_case", label: "กรณีดีที่สุด", tone: "border-emerald-500/30" },
  { key: "likely_case", label: "กรณีที่น่าจะเป็น", tone: "border-blue-500/30" },
  { key: "worst_case", label: "กรณีเลวร้ายที่สุด", tone: "border-red-500/30" },
] as const;

export default function Scenarios() {
  const { data, error, loading } = usePoll(api.scenarios, 60000);

  const banner = (
    <p className="card border-amber-500/30 bg-amber-500/5 px-4 py-2.5 text-xs text-amber-200/90">
      {DISCLAIMER}
    </p>
  );

  if (error?.includes("Not Found")) {
    return (
      <div className="space-y-3">
        {banner}
        <Empty
          title="ยังไม่มีฉากทัศน์"
          hint="หน้านี้จะมีข้อมูลเมื่อ horizon-reasoner (Phase 3) ทำงาน — สร้างฉากทัศน์ด้วย RAG จากคลังเหตุการณ์เมื่อมีสัญญาณเข้ามา"
        />
      </div>
    );
  }
  if (error) return <ErrorBox message={error} />;
  if (loading) return <p className="text-sm text-slate-500">กำลังโหลด…</p>;

  return (
    <div className="space-y-3">
      {banner}
      {!data?.length && (
        <Empty
          title="ยังไม่มีฉากทัศน์"
          hint="ฉากทัศน์จะถูกสร้างเมื่อมีสัญญาณอ่อนหรือ trend breakout ส่งเข้าช่อง horizon:signals"
        />
      )}

      {data?.map((scenario) => (
        <article key={scenario.id} className="card px-4 py-4">
          <header className="mb-3 flex flex-wrap items-baseline gap-2">
            <h2 className="text-sm font-semibold text-slate-100">
              {scenario.label ?? "(ยังไม่มีชื่อคลัสเตอร์)"}
            </h2>
            <span className="text-[11px] text-slate-600">
              {fmtTime(scenario.created_at)} · {scenario.model ?? "—"} ·{" "}
              อ้างอิง {scenario.source_event_ids.length} เหตุการณ์
            </span>
          </header>

          <div className="grid gap-3 md:grid-cols-3">
            {CASES.map(({ key, label, tone }) => (
              <div key={key} className={`rounded border ${tone} bg-ink-700/50 px-3 py-2.5`}>
                <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                  {label}
                </p>
                <p className="text-sm leading-relaxed text-slate-300">{scenario[key] ?? "—"}</p>
              </div>
            ))}
          </div>

          {scenario.indicators.length > 0 && (
            <div className="mt-3">
              <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                ตัวชี้วัดที่ต้องเฝ้าดู
              </p>
              <ul className="space-y-1">
                {scenario.indicators.map((indicator, i) => (
                  <li key={i} className="flex gap-2 text-sm text-slate-300">
                    <span className="chip mt-0.5 shrink-0 bg-slate-500/15 text-slate-400">
                      {indicator.watch_type}
                    </span>
                    <span>{indicator.description}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </article>
      ))}
    </div>
  );
}
