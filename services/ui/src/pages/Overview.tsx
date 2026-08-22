import { api, usePoll } from "../api";
import { Category, Chip, Credibility, Empty, ErrorBox, Stat, fmtAgo, fmtTime } from "../components/ui";

const STATUS_LABEL: Record<string, string> = {
  processed: "ประมวลผลแล้ว",
  queued: "รอคิว",
  dropped_duplicate: "ซ้ำ (ตัดออก)",
  dropped_lowcred: "ความน่าเชื่อถือต่ำ",
  failed: "ล้มเหลว",
};

const STATUS_TONE: Record<string, string> = {
  processed: "text-emerald-300",
  queued: "text-amber-300",
  dropped_duplicate: "text-slate-400",
  dropped_lowcred: "text-slate-500",
  failed: "text-red-300",
};

export default function Overview() {
  const stats = usePoll(api.stats, 10000);
  const events = usePoll(() => api.events(40), 15000);

  return (
    <div className="space-y-6">
      {stats.error && <ErrorBox message={stats.error} />}

      <section className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="เหตุการณ์ทั้งหมด" value={stats.data?.events_total ?? "—"} />
        <Stat
          label="24 ชม. ล่าสุด"
          value={stats.data?.events_last_24h ?? "—"}
          tone="text-emerald-300"
        />
        <Stat
          label="คิวรอประมวลผล"
          value={stats.data?.queue_depth ?? "—"}
          tone={stats.data && stats.data.queue_depth > 0 ? "text-amber-300" : undefined}
        />
        <Stat label="ข้อมูลไม่ครบ" value={stats.data?.events_incomplete ?? "—"} />
        <Stat label="แหล่งข่าวที่เปิดใช้" value={stats.data?.sources_active ?? "—"} />
      </section>

      {stats.data && (
        <section className="card px-4 py-3">
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
            สถานะบทความที่ดึงเข้ามา
          </h2>
          <div className="flex flex-wrap gap-x-6 gap-y-1.5">
            {Object.entries(stats.data.articles_by_status)
              .sort((a, b) => b[1] - a[1])
              .map(([status, count]) => (
                <span key={status} className="text-sm text-slate-400">
                  {STATUS_LABEL[status] ?? status}{" "}
                  <span className={`font-mono ${STATUS_TONE[status] ?? "text-slate-300"}`}>
                    {count}
                  </span>
                </span>
              ))}
          </div>
        </section>
      )}

      <section>
        <h2 className="mb-2 flex items-baseline gap-2 text-sm font-semibold text-slate-300">
          เหตุการณ์ล่าสุด
          <span className="text-[11px] font-normal text-slate-600">อัปเดตอัตโนมัติทุก 15 วินาที</span>
        </h2>

        {events.error && <ErrorBox message={events.error} />}
        {events.data?.length === 0 && (
          <Empty
            title="ยังไม่มีเหตุการณ์"
            hint="poller ดึงข่าวทุก 15 นาที จากนั้น worker จะสกัดเหตุการณ์ออกมา รอสักครู่แล้วรีเฟรช"
          />
        )}

        <div className="space-y-2">
          {events.data?.map((event) => (
            <article key={event.id} className="card px-4 py-3">
              <div className="mb-1.5 flex flex-wrap items-center gap-2">
                {event.categories.map((c) => (
                  <Category key={c} name={c} />
                ))}
                {event.source_count > 1 && (
                  <Chip tone="blue">{event.source_count} แหล่ง</Chip>
                )}
                {event.updates > 0 && <Chip tone="amber">อัปเดต {event.updates} ครั้ง</Chip>}
                {event.incomplete && <Chip tone="red">ข้อมูลไม่ครบ</Chip>}
                <span className="ml-auto text-[11px] text-slate-600" title={fmtTime(event.created_at)}>
                  {fmtAgo(event.created_at)}
                </span>
              </div>

              <p className="text-sm leading-relaxed text-slate-200">{event.summary}</p>

              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-500">
                {event.actors.length > 0 && (
                  <span className="max-w-xl truncate" title={event.actors.join(", ")}>
                    ผู้เกี่ยวข้อง: <span className="text-slate-400">{event.actors.join(" · ")}</span>
                  </span>
                )}
                {event.location && <span>สถานที่: {event.location}</span>}
                <span>เวลาเหตุการณ์: {fmtTime(event.event_time)}</span>
                <Credibility value={event.credibility_weight} />
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
