import type { ReactNode } from "react";

export function fmtAgo(iso: string | null): string {
  if (!iso) return "—";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 60) return "เมื่อครู่";
  if (secs < 3600) return `${Math.floor(secs / 60)} นาทีที่แล้ว`;
  if (secs < 86400) return `${Math.floor(secs / 3600)} ชม.ที่แล้ว`;
  return `${Math.floor(secs / 86400)} วันที่แล้ว`;
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
