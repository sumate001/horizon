import { api, usePoll } from "../api";
import { Category, Chip, Empty, ErrorBox, Sparkline, fmtAgo } from "../components/ui";

function scoreTone(score: number | null) {
  if (score === null) return "text-slate-500";
  if (score >= 2.5) return "text-red-300";
  if (score >= 1.5) return "text-amber-300";
  return "text-slate-300";
}

export default function Trends() {
  const { data, error, loading } = usePoll(api.trends, 60000);

  // The endpoint only exists once the batch service ships (phase 2).
  if (error?.includes("Not Found")) {
    return (
      <Empty
        title="ยังไม่มีการจัดกลุ่มเหตุการณ์"
        hint="หน้านี้จะมีข้อมูลเมื่อ horizon-batch (Phase 2) ทำงาน — clustering ด้วย HDBSCAN แล้วคำนวณ trend score ทุก 3 ชั่วโมง"
      />
    );
  }
  if (error) return <ErrorBox message={error} />;
  if (loading) return <p className="text-sm text-slate-500">กำลังโหลด…</p>;
  if (!data?.length) {
    return (
      <Empty
        title="ยังไม่มีคลัสเตอร์ที่จัดกลุ่มได้"
        hint="ต้องมีเหตุการณ์อย่างน้อย MIN_CLUSTER_SIZE (ค่าเริ่มต้น 4) ที่เกี่ยวข้องกันจึงจะเกิดคลัสเตอร์"
      />
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-2">
        <h1 className="text-sm font-semibold text-slate-300">แนวโน้มเรียงตาม trend score</h1>
        <span className="text-[11px] text-slate-600">
          trend_score = 0.3·z(ความถี่) + 0.4·z(ความเร็ว) + 0.3·z(ความเร่ง) · เกณฑ์ breakout ≥ 2.5
        </span>
      </div>

      <div className="card overflow-hidden">
        <table className="w-full">
          <thead className="border-b border-ink-500/60">
            <tr>
              <th className="th">คลัสเตอร์</th>
              <th className="th">หมวด</th>
              <th className="th w-24">เหตุการณ์</th>
              <th className="th w-28">แนวโน้ม</th>
              <th className="th w-40">z (ถี่/เร็ว/เร่ง)</th>
              <th className="th w-28 text-right">trend score</th>
              <th className="th w-28">ล่าสุด</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ink-500/40">
            {data.map((row) => (
              <tr key={row.cluster_id} className="hover:bg-ink-700/40">
                <td className="td max-w-md">
                  <p className="truncate text-slate-200">{row.label ?? "(ยังไม่มีชื่อคลัสเตอร์)"}</p>
                  {row.provisional && (
                    <span className="mt-1 inline-block">
                      <Chip tone="amber">ชั่วคราว — ประวัติไม่ถึง 14 วัน</Chip>
                    </span>
                  )}
                </td>
                <td className="td">
                  <div className="flex flex-wrap gap-1">
                    {row.categories.map((c) => (
                      <Category key={c} name={c} />
                    ))}
                  </div>
                </td>
                <td className="td font-mono text-slate-300">{row.event_count}</td>
                <td className="td">
                  <Sparkline points={row.history} />
                </td>
                <td className="td font-mono text-[11px] text-slate-400">
                  {[row.z_frequency, row.z_velocity, row.z_acceleration]
                    .map((z) => (z === null ? "—" : z.toFixed(1)))
                    .join(" / ")}
                </td>
                <td className={`td text-right font-mono text-base ${scoreTone(row.trend_score)}`}>
                  {row.trend_score === null ? "—" : row.trend_score.toFixed(2)}
                </td>
                <td className="td text-[11px] text-slate-500">{fmtAgo(row.last_seen)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
