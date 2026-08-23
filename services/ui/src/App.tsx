import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { api, usePoll } from "./api";
import Entities from "./pages/Entities";
import Ops from "./pages/Ops";
import Sources from "./pages/Sources";
import Triage from "./pages/Triage";

// Horizon is the engine; analysts work in OSINT//DESK. This nav is for whoever
// keeps the engine running, which is why there is nothing here about trends,
// weak signals or scenarios — those surface to humans on the other side.
const NAV = [
  { to: "/ops", label: "สถานะระบบ" },
  { to: "/entities", label: "ทะเบียนตัวตน" },
  { to: "/triage", label: "ปรับสูตรคะแนน" },
  { to: "/sources", label: "แหล่งข่าว" },
];

function Header() {
  // Doubles as a liveness indicator: if the API is down the pill goes red.
  const { data, error } = usePoll(api.stats, 15000);
  return (
    <header className="sticky top-0 z-10 border-b border-ink-500/60 bg-ink-900/95 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center gap-6 px-5 py-3">
        <div className="flex items-baseline gap-2">
          <span className="text-lg font-bold tracking-tight text-slate-100">Horizon</span>
          <span className="text-[11px] text-slate-500">engine · หน้าจอผู้ดูแลระบบ</span>
        </div>
        <nav className="flex gap-1">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `rounded px-3 py-1.5 text-sm transition-colors ${
                  isActive
                    ? "bg-ink-600 text-slate-100"
                    : "text-slate-400 hover:bg-ink-700 hover:text-slate-200"
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        {/* Plain HTML, served straight from public/ — nothing to build or route. */}
        <a
          href="/workflow.html"
          className="ml-auto rounded px-2.5 py-1.5 text-[11px] text-slate-500 transition-colors hover:bg-ink-700 hover:text-slate-300"
        >
          ระบบนี้ทำงานอย่างไร
        </a>
        <div className="flex items-center gap-3 text-[11px] text-slate-500">
          {error ? (
            <span className="chip bg-red-500/15 text-red-300">API ไม่ตอบสนอง</span>
          ) : (
            data && (
              <>
                <span>
                  คิว <span className="font-mono text-slate-300">{data.queue_depth}</span>
                </span>
                <span>
                  เหตุการณ์ <span className="font-mono text-slate-300">{data.events_total}</span>
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                  ทำงานปกติ
                </span>
              </>
            )
          )}
        </div>
      </div>
    </header>
  );
}

export default function App() {
  return (
    <div className="min-h-screen">
      <Header />
      <main className="mx-auto max-w-7xl px-5 py-6">
        <Routes>
          <Route path="/" element={<Navigate to="/ops" replace />} />
          <Route path="/ops" element={<Ops />} />
          <Route path="/entities" element={<Entities />} />
          <Route path="/triage" element={<Triage />} />
          <Route path="/sources" element={<Sources />} />
          {/* Old analyst routes — anyone with a bookmark lands on ops. */}
          <Route path="*" element={<Navigate to="/ops" replace />} />
        </Routes>
      </main>
    </div>
  );
}
