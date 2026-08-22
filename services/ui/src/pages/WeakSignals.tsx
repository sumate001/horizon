import { api, usePoll } from "../api";
import { Category, Chip, Empty, ErrorBox, ScoreBar, fmtAgo } from "../components/ui";

const STATUS: Record<string, { label: string; tone: string }> = {
  candidate: { label: "รอพิจารณา", tone: "slate" },
  dispatched: { label: "ส่งไป OSINT//DESK แล้ว", tone: "blue" },
  verified_true: { label: "ยืนยันว่าจริง", tone: "green" },
  verified_false: { label: "ยืนยันว่าไม่จริง", tone: "red" },
  expired: { label: "หมดอายุ", tone: "slate" },
};

export default function WeakSignals() {
  const { data, error, loading } = usePoll(api.weakSignals, 60000);

  if (error?.includes("Not Found")) {
    return (
      <Empty
        title="ยังไม่มีการตรวจจับสัญญาณอ่อน"
        hint="หน้านี้จะมีข้อมูลเมื่อ horizon-batch (Phase 2) ทำงาน — novelty + Isolation Forest + Kleinberg burst"
      />
    );
  }
  if (error) return <ErrorBox message={error} />;
  if (loading) return <p className="text-sm text-slate-500">กำลังโหลด…</p>;
  if (!data?.length) {
    return (
      <Empty
        title="ยังไม่พบสัญญาณอ่อน"
        hint="ผู้สมัครคือเหตุการณ์ที่ไม่เข้าคลัสเตอร์ใด ๆ และคลัสเตอร์ที่มีเหตุการณ์ไม่เกิน 5 รายการ ต้องได้คะแนนรวม ≥ 0.65 จึงจะถูกบันทึก"
      />
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-2">
        <h1 className="text-sm font-semibold text-slate-300">สัญญาณอ่อน</h1>
        <span className="text-[11px] text-slate-600">
          คะแนนรวม = (0.4·ความแปลกใหม่ + 0.3·ความโดดเดี่ยว + 0.3·การปะทุ) × ความน่าเชื่อถือเฉลี่ย
        </span>
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        {data.map((signal) => {
          const status = STATUS[signal.status] ?? { label: signal.status, tone: "slate" };
          return (
            <article key={signal.id} className="card px-4 py-3">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <Chip tone={status.tone}>{status.label}</Chip>
                {signal.categories.map((c) => (
                  <Category key={c} name={c} />
                ))}
                <span className="ml-auto font-mono text-lg text-cyan-300">
                  {signal.combined_score.toFixed(2)}
                </span>
              </div>

              <p className="mb-3 text-sm leading-relaxed text-slate-200">
                {signal.title ?? "(ไม่มีคำอธิบาย)"}
              </p>

              <div className="space-y-1.5">
                <ScoreBar label="แปลกใหม่" value={signal.novelty_score} />
                <ScoreBar label="โดดเดี่ยว" value={signal.isolation_score} />
                <ScoreBar label="ปะทุ" value={signal.burst_score} />
              </div>

              <p className="mt-2 text-[11px] text-slate-600">พบเมื่อ {fmtAgo(signal.created_at)}</p>
            </article>
          );
        })}
      </div>
    </div>
  );
}
