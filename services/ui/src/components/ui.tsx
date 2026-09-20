import { useState } from "react";
import type { ReactNode } from "react";

import { apiKey, setApiKey } from "../api";

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


/**
 * Where the API key gets entered — rendered wherever it is actually needed.
 *
 * Every write endpoint needs HORIZON_API_KEY, and it used to live on the
 * Sources page alone. Other pages rendered their buttons `disabled` with an
 * 11px slate-600 line elsewhere on the page saying to go and set it, which
 * reads as a broken button rather than a locked one: the entity review queue
 * showed ถูกต้อง / รวมผิด greyed out and nothing on the screen was obviously
 * the reason. The queue was unusable and looked like it was unusable by mistake.
 *
 * `onSaved` exists because apiKey() is read during render: without telling the
 * page, saving the key leaves the buttons disabled until the next poll.
 */
export function ApiKeyBar({ note, onSaved }: { note?: string; onSaved?: () => void }) {
  const [key, setKey] = useState(apiKey() ?? "");
  const [saved, setSaved] = useState(false);
  return (
    <div className="card border-amber-500/30 bg-amber-500/5 px-4 py-2.5">
      {note && <p className="mb-2 text-[11px] text-amber-300/90">{note}</p>}
      <div className="flex items-center gap-2">
        <label className="text-[11px] uppercase tracking-wide text-slate-500">API key</label>
        <input
          type="password"
          value={key}
          onChange={(e) => {
            setKey(e.target.value);
            setSaved(false);
          }}
          placeholder="HORIZON_API_KEY (จำเป็นเฉพาะตอนแก้ไข)"
          className="flex-1 rounded border border-ink-500 bg-ink-700 px-2 py-1 font-mono text-xs text-slate-200 outline-none focus:border-cyan-500/60"
        />
        <button
          onClick={() => {
            setApiKey(key);
            setSaved(true);
            onSaved?.();
          }}
          className="rounded bg-ink-600 px-3 py-1 text-xs text-slate-200 hover:bg-ink-500"
        >
          {saved ? "บันทึกแล้ว" : "บันทึก"}
        </button>
      </div>
    </div>
  );
}
