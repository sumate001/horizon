import { useState } from "react";

import { api, apiKey, setApiKey, usePoll, type Source } from "../api";
import { Chip, Credibility, ErrorBox } from "../components/ui";

const BLANK = { name: "", url: "", type: "rss" as const, credibility_weight: 0.7, active: true };

/** Write endpoints require HORIZON_API_KEY; it is held in localStorage. */
function KeyBar() {
  const [key, setKey] = useState(apiKey() ?? "");
  const [saved, setSaved] = useState(false);
  return (
    <div className="card flex items-center gap-2 px-4 py-2.5">
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
        }}
        className="rounded bg-ink-600 px-3 py-1 text-xs text-slate-200 hover:bg-ink-500"
      >
        {saved ? "บันทึกแล้ว" : "บันทึก"}
      </button>
    </div>
  );
}

export default function Sources() {
  const { data, error, reload } = usePoll(api.sources, 30000);
  const [draft, setDraft] = useState(BLANK);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function run(id: string, action: () => Promise<unknown>) {
    setBusy(id);
    setActionError(null);
    try {
      await action();
      await reload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  const patch = (source: Source, body: Partial<Source>) =>
    run(source.id, () => api.updateSource(source.id, body));

  return (
    <div className="space-y-3">
      <KeyBar />
      {error && <ErrorBox message={error} />}
      {actionError && <ErrorBox message={actionError} />}

      <div className="card overflow-hidden">
        <table className="w-full">
          <thead className="border-b border-ink-500/60">
            <tr>
              <th className="th">แหล่งข่าว</th>
              <th className="th">URL</th>
              <th className="th w-28">ประเภท</th>
              <th className="th w-56">ความน่าเชื่อถือ</th>
              <th className="th w-24">สถานะ</th>
              <th className="th w-20"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ink-500/40">
            {data?.map((source) => (
              <tr key={source.id} className={busy === source.id ? "opacity-50" : ""}>
                <td className="td text-slate-200">{source.name}</td>
                <td className="td max-w-xs truncate font-mono text-[11px] text-slate-500">
                  {source.url}
                </td>
                <td className="td text-[11px] text-slate-400">{source.type}</td>
                <td className="td">
                  <div className="flex items-center gap-2">
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      defaultValue={source.credibility_weight}
                      onMouseUp={(e) =>
                        patch(source, {
                          credibility_weight: Number((e.target as HTMLInputElement).value),
                        })
                      }
                      className="h-1 w-24 accent-cyan-400"
                    />
                    <Credibility value={source.credibility_weight} />
                  </div>
                </td>
                <td className="td">
                  <button onClick={() => patch(source, { active: !source.active })}>
                    <Chip tone={source.active ? "green" : "slate"}>
                      {source.active ? "เปิดใช้" : "ปิดอยู่"}
                    </Chip>
                  </button>
                </td>
                <td className="td text-right">
                  <button
                    onClick={() => {
                      if (confirm(`ลบแหล่งข่าว "${source.name}" ?`))
                        run(source.id, () => api.deleteSource(source.id));
                    }}
                    className="text-[11px] text-slate-600 hover:text-red-400"
                  >
                    ลบ
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          run("new", async () => {
            await api.createSource(draft);
            setDraft(BLANK);
          });
        }}
        className="card flex flex-wrap items-end gap-2 px-4 py-3"
      >
        <div className="flex-1">
          <label className="mb-1 block text-[11px] uppercase tracking-wide text-slate-500">ชื่อ</label>
          <input
            required
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            className="w-full rounded border border-ink-500 bg-ink-700 px-2 py-1 text-sm outline-none focus:border-cyan-500/60"
          />
        </div>
        <div className="flex-[2]">
          <label className="mb-1 block text-[11px] uppercase tracking-wide text-slate-500">
            URL (หรือ query สำหรับ searxng)
          </label>
          <input
            required
            value={draft.url}
            onChange={(e) => setDraft({ ...draft, url: e.target.value })}
            className="w-full rounded border border-ink-500 bg-ink-700 px-2 py-1 font-mono text-xs outline-none focus:border-cyan-500/60"
          />
        </div>
        <div>
          <label className="mb-1 block text-[11px] uppercase tracking-wide text-slate-500">
            ความน่าเชื่อถือ
          </label>
          <input
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={draft.credibility_weight}
            onChange={(e) => setDraft({ ...draft, credibility_weight: Number(e.target.value) })}
            className="w-24 rounded border border-ink-500 bg-ink-700 px-2 py-1 font-mono text-sm outline-none focus:border-cyan-500/60"
          />
        </div>
        <button
          type="submit"
          className="rounded bg-cyan-600/80 px-4 py-1.5 text-sm font-medium text-white hover:bg-cyan-600"
        >
          เพิ่มแหล่งข่าว
        </button>
      </form>

      <p className="text-[11px] text-slate-600">
        แหล่งข่าวที่ความน่าเชื่อถือต่ำกว่า 0.20 จะถูกกรองทิ้งตั้งแต่ก่อนเรียก LLM
        (บันทึกเป็น dropped_lowcred และไม่กลายเป็นเหตุการณ์)
      </p>
    </div>
  );
}
