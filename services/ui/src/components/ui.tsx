import type { ReactNode } from "react";

/** Display timezone is Asia/Bangkok; everything from the API is UTC. */
const BKK = "Asia/Bangkok";

export function fmtTime(iso: string | null, withDate = true): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("th-TH", {
    timeZone: BKK,
    // Full year: a 2-digit Buddhist-era year ("69") reads as 1969 next to the
    // Gregorian years the LLM writes into summaries.
    ...(withDate ? { day: "2-digit", month: "short", year: "numeric" } : {}),
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtAgo(iso: string | null): string {
  if (!iso) return "—";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 60) return "เมื่อครู่";
  if (secs < 3600) return `${Math.floor(secs / 60)} นาทีที่แล้ว`;
  if (secs < 86400) return `${Math.floor(secs / 3600)} ชม.ที่แล้ว`;
  return `${Math.floor(secs / 86400)} วันที่แล้ว`;
}

const CATEGORY_TONE: Record<string, string> = {
  การเมือง: "bg-rose-500/15 text-rose-300",
  เศรษฐกิจ: "bg-amber-500/15 text-amber-300",
  ความมั่นคง: "bg-red-500/15 text-red-300",
  เทคโนโลยี: "bg-cyan-500/15 text-cyan-300",
  สังคม: "bg-violet-500/15 text-violet-300",
  สิ่งแวดล้อม: "bg-emerald-500/15 text-emerald-300",
  ต่างประเทศ: "bg-blue-500/15 text-blue-300",
  พลังงาน: "bg-orange-500/15 text-orange-300",
  "บันเทิง/กีฬา": "bg-pink-500/15 text-pink-300",
};

export function Category({ name }: { name: string }) {
  return (
    <span className={`chip ${CATEGORY_TONE[name] ?? "bg-slate-500/15 text-slate-300"}`}>{name}</span>
  );
}

export function Chip({ tone = "slate", children }: { tone?: string; children: ReactNode }) {
  const tones: Record<string, string> = {
    slate: "bg-slate-500/15 text-slate-300",
    green: "bg-emerald-500/15 text-emerald-300",
    amber: "bg-amber-500/15 text-amber-300",
    red: "bg-red-500/15 text-red-300",
    blue: "bg-blue-500/15 text-blue-300",
  };
  return <span className={`chip ${tones[tone] ?? tones.slate}`}>{children}</span>;
}

/** Credibility rendered as a bar — easier to scan down a column than a number. */
export function Credibility({ value }: { value: number }) {
  const tone = value >= 0.8 ? "bg-emerald-400" : value >= 0.5 ? "bg-amber-400" : "bg-red-400";
  return (
    <span className="inline-flex items-center gap-1.5" title={`credibility ${value.toFixed(2)}`}>
      <span className="h-1 w-10 overflow-hidden rounded bg-ink-500">
        <span className={`block h-full ${tone}`} style={{ width: `${value * 100}%` }} />
      </span>
      <span className="font-mono text-[11px] text-slate-400">{value.toFixed(2)}</span>
    </span>
  );
}

export function ScoreBar({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center gap-2">
      <span className="w-16 shrink-0 text-[11px] text-slate-500">{label}</span>
      <span className="h-1.5 flex-1 overflow-hidden rounded bg-ink-500">
        <span className="block h-full bg-cyan-400" style={{ width: `${Math.min(value, 1) * 100}%` }} />
      </span>
      <span className="w-9 shrink-0 text-right font-mono text-[11px] text-slate-400">
        {value.toFixed(2)}
      </span>
    </div>
  );
}

export function Sparkline({ points, className = "" }: { points: number[]; className?: string }) {
  if (points.length < 2) return <span className="text-[11px] text-slate-600">ข้อมูลไม่พอ</span>;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const d = points
    .map((p, i) => {
      const x = (i / (points.length - 1)) * 100;
      const y = 24 - ((p - min) / span) * 22 - 1;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const rising = points[points.length - 1] >= points[0];
  return (
    <svg viewBox="0 0 100 24" preserveAspectRatio="none" className={`h-6 w-24 ${className}`}>
      <path d={d} fill="none" strokeWidth={1.5} className={rising ? "stroke-emerald-400" : "stroke-slate-500"} />
    </svg>
  );
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="card flex flex-col items-center gap-1 px-6 py-14 text-center">
      <p className="text-sm text-slate-400">{title}</p>
      {hint && <p className="max-w-md text-xs text-slate-600">{hint}</p>}
    </div>
  );
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="card border-red-500/40 bg-red-500/5 px-4 py-3 text-sm text-red-300">
      โหลดข้อมูลไม่สำเร็จ — {message}
    </div>
  );
}

export function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="card px-4 py-3">
      <p className="text-[11px] uppercase tracking-wide text-slate-500">{label}</p>
      <p className={`mt-1 font-mono text-2xl ${tone ?? "text-slate-100"}`}>{value}</p>
    </div>
  );
}
