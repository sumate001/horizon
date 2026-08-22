import { useCallback, useEffect, useRef, useState } from "react";

// nginx proxies /api to horizon-api, so everything is same-origin.
const BASE = "/api/v1";

export type Stats = {
  queue_depth: number;
  articles_by_status: Record<string, number>;
  events_total: number;
  events_last_24h: number;
  events_incomplete: number;
  sources_active: number;
};

export type EventRow = {
  id: string;
  summary: string | null;
  actors: string[];
  action: string | null;
  location: string | null;
  event_time: string | null;
  categories: string[];
  source_count: number;
  credibility_weight: number;
  incomplete: boolean;
  updates: number;
  created_at: string;
};

export type Source = {
  id: string;
  name: string;
  url: string;
  type: "rss" | "searxng" | "watchlist_hook";
  credibility_weight: number;
  active: boolean;
  created_at: string;
};

export type TrendRow = {
  cluster_id: string;
  label: string | null;
  event_count: number;
  status: string;
  first_seen: string | null;
  last_seen: string | null;
  trend_score: number | null;
  z_frequency: number | null;
  z_velocity: number | null;
  z_acceleration: number | null;
  provisional: boolean;
  history: number[];
  categories: string[];
};

export type WeakSignalRow = {
  id: string;
  event_id: string | null;
  cluster_id: string | null;
  title: string | null;
  novelty_score: number;
  isolation_score: number;
  burst_score: number;
  combined_score: number;
  status: string;
  created_at: string;
  categories: string[];
};

export type ScenarioRow = {
  id: string;
  cluster_id: string;
  label: string | null;
  best_case: string | null;
  worst_case: string | null;
  likely_case: string | null;
  indicators: { description: string; watch_type: string }[];
  source_event_ids: string[];
  model: string | null;
  created_at: string;
};

class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(apiKey() ? { "X-API-Key": apiKey()! } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    throw new ApiError(await response.text().catch(() => response.statusText), response.status);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

// Write endpoints need HORIZON_API_KEY. Kept in localStorage rather than baked
// into the bundle so the same image works across deployments.
export const apiKey = () => localStorage.getItem("horizon_api_key");
export const setApiKey = (key: string) =>
  key ? localStorage.setItem("horizon_api_key", key) : localStorage.removeItem("horizon_api_key");

export const api = {
  stats: () => request<Stats>("/stats"),
  events: (limit = 50) => request<EventRow[]>(`/events?limit=${limit}`),
  sources: () => request<Source[]>("/sources"),
  createSource: (body: Partial<Source>) =>
    request<Source>("/sources", { method: "POST", body: JSON.stringify(body) }),
  updateSource: (id: string, body: Partial<Source>) =>
    request<Source>(`/sources/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteSource: (id: string) => request<void>(`/sources/${id}`, { method: "DELETE" }),
  trends: () => request<TrendRow[]>("/trends"),
  weakSignals: () => request<WeakSignalRow[]>("/weak-signals"),
  scenarios: () => request<ScenarioRow[]>("/scenarios"),
};

type Poll<T> = { data: T | null; error: string | null; loading: boolean; reload: () => void };

/** Fetch on mount, then re-fetch every `intervalMs` (0 disables polling). */
export function usePoll<T>(fetcher: () => Promise<T>, intervalMs = 0, deps: unknown[] = []): Poll<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Keeps the interval from restarting when the caller passes a new closure.
  const ref = useRef(fetcher);
  ref.current = fetcher;

  const load = useCallback(async () => {
    try {
      setData(await ref.current());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    if (!intervalMs) return;
    const timer = setInterval(load, intervalMs);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, intervalMs, ...deps]);

  return { data, error, loading, reload: load };
}
