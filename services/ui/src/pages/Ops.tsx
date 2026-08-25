import { api, usePoll } from "../api";
import { Empty, ErrorBox, Stat, fmtAgo } from "../components/ui";

/**
 * Pipeline health.
 *
 * Horizon is an engine now — analysts work in OSINT//DESK. This page exists for
 * whoever keeps the engine running, so it answers operational questions only:
 * is work moving, is anything failing, are the sources alive.
 */

const STATUS_LABEL: Record<string, string> = {
  processed: "ประมวลผลแล้ว",
  queued: "รอคิว",
  dropped_duplicate: "ซ้ำ (รวมเข้าเหตุการณ์เดิม)",
  dropped_lowcred: "แหล่งความน่าเชื่อถือต่ำ",
  failed: "ล้มเหลว",
};

const STATUS_TONE: Record<string, string> = {
  processed: "text-emerald-300",
  queued: "text-amber-300",
  dropped_duplicate: "text-cyan-300",
  dropped_lowcred: "text-slate-500",
  failed: "text-red-300",
};

function Bar({ label, value, total, tone }: { label: string; value: number; total: number; tone: string }) {
  const pct = total > 0 ? (value / total) * 100 : 0;
  return (
    <div className="flex items-center gap-3">
      <span className="w-52 shrink-0 text-sm text-slate-400">{label}</span>
      <span className="h-2 flex-1 overflow-hidden rounded bg-ink-600">
        <span className={`block h-full ${tone}`} style={{ width: `${pct}%` }} />
      </span>
      <span className="w-24 shrink-0 text-right font-mono text-sm text-slate-300">
        {value} <span className="text-[11px] text-slate-600">{pct.toFixed(0)}%</span>
      </span>
    </div>
  );
}

export default function Ops() {
  const stats = usePoll(api.stats, 10000);
  const events = usePoll(() => api.events(1), 30000);

  const byStatus = stats.data?.articles_by_status ?? {};
  const totalArticles = Object.values(byStatus).reduce((a, b) => a + b, 0);
  const failed = byStatus.failed ?? 0;
  const deduped = byStatus.dropped_duplicate ?? 0;
  const detection = stats.data?.detection;
  const trendReady = detection?.trend_ready_at
    ? new Date(detection.trend_ready_at).toLocaleDateString("th-TH", {
        day: "numeric",
        month: "long",
      })
    : null;

  return (
    <div className="space-y-6">
      {stats.error && <ErrorBox message={stats.error} />}

      <section className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat
          label="คิวรอประมวลผล"
          value={stats.data?.queue_depth ?? "—"}
          tone={stats.data && stats.data.queue_depth > 500 ? "text-red-300" : undefined}
        />
        <Stat label="เหตุการณ์ทั้งหมด" value={stats.data?.events_total ?? "—"} />
        <Stat label="24 ชม. ล่าสุด" value={stats.data?.events_last_24h ?? "—"} tone="text-emerald-300" />
        <Stat
          label="ล้มเหลว"
          value={failed}
          tone={failed > 0 ? "text-red-300" : undefined}
        />
        <Stat label="แหล่งข่าวที่เปิดใช้" value={stats.data?.sources_active ?? "—"} />
      </section>

      <section className="card px-5 py-4">
        <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-500">
          การตรวจจับ
        </h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
          <Stat
            label="สัญญาณอ่อนที่รอตรวจ"
            value={detection?.weak_signals_open ?? "—"}
            tone={detection?.weak_signals_open ? "text-amber-300" : undefined}
          />
          <Stat label="แนวโน้มพุ่ง 24 ชม." value={detection?.breakouts_last_24h ?? "—"} />
          <Stat
            label="คลัสเตอร์ที่ยังนับไม่ได้"
            value={detection ? `${detection.clusters_provisional}/${detection.clusters_total}` : "—"}
          />
        </div>
        {/* Ingestion health said "everything is fine" for three days while the
            detector emitted nothing, because nothing reported on the detector.
            A count of zero is ambiguous; this says which zero it is. */}
        <p className="mt-3 text-[11px] text-slate-600">
          {trendReady
            ? `ยังไม่มีคลัสเตอร์ไหนมีประวัติครบ 14 วัน — z-score บนหน้าต่างไม่กี่บานบอกเรื่องขนาดตัวอย่าง ไม่ใช่เรื่องโลกจริง แนวโน้มพุ่งจะเริ่มรายงานได้ ${trendReady}`
            : "มีคลัสเตอร์ที่ประวัติยาวพอให้อ่าน z-score ได้แล้ว"}
        </p>
      </section>

      <section className="card px-5 py-4">
        <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-500">
          ปลายทางของบทความที่ดึงเข้ามา
        </h2>
        <div className="space-y-2">
          {Object.entries(byStatus)
            .sort((a, b) => b[1] - a[1])
            .map(([status, count]) => (
              <Bar
                key={status}
                label={STATUS_LABEL[status] ?? status}
                value={count}
                total={totalArticles}
                tone={(STATUS_TONE[status] ?? "text-slate-400").replace("text-", "bg-")}
              />
            ))}
        </div>
        <p className="mt-3 text-[11px] text-slate-600">
          {deduped > 0
            ? `รวมข่าวซ้ำได้ ${deduped} ชิ้น — ตัวเลขนี้คือสิ่งที่ระบบทำให้ ไม่ใช่ของที่หายไป`
            : "ยังไม่พบข่าวซ้ำ"}
          {stats.data?.events_incomplete
            ? ` · ข้อมูลไม่ครบ ${stats.data.events_incomplete} เหตุการณ์ (ถูกกันออกจากการจัดกลุ่ม)`
            : ""}
        </p>
      </section>

      <section className="grid gap-3 md:grid-cols-2">
        <div className="card px-5 py-4">
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
            สายพานล่าสุด
          </h2>
          {events.data?.length ? (
            <p className="text-sm text-slate-300">
              เหตุการณ์ล่าสุดเมื่อ{" "}
              <span className="text-slate-100">{fmtAgo(events.data[0].created_at)}</span>
            </p>
          ) : (
            <p className="text-sm text-slate-500">ยังไม่มีเหตุการณ์</p>
          )}
          <p className="mt-1 text-[11px] text-slate-600">
            poller ดึงทุก 15 นาที · batch จัดกลุ่มทุก 3 ชม. · reasoner ตื่นเมื่อมีสัญญาณ
          </p>
        </div>

        <div className="card px-5 py-4">
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
            Metrics ต่อ service
          </h2>
          <p className="text-[11px] leading-relaxed text-slate-500">
            แต่ละ service เปิด endpoint ของตัวเอง — endpoint เดียวรวมจะรายงานแค่ process
            ที่ตอบ ทำให้ตัวเลขของ worker และ reasoner ขึ้นศูนย์ทั้งที่ทำงานอยู่
          </p>
          <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-[11px] text-slate-400">
            <span>api :8300/metrics</span>
            <span>poller :9101</span>
            <span>worker :9102</span>
            <span>batch :9103</span>
            <span>reasoner :9104</span>
          </div>
        </div>
      </section>

      {!stats.data && !stats.error && <Empty title="กำลังเชื่อมต่อ…" />}
    </div>
  );
}
