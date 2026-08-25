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
  detection: {
    weak_signals_open: number;
    clusters_total: number;
    clusters_provisional: number;
    /** When a trend breakout first becomes possible; null once one has matured. */
    trend_ready_at: string | null;
    breakouts_last_24h: number;
  };
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

export type TriageSettings = {
  sensitivity_coefficient: number;
  priority_total: number;
  priority_urgency: number;
  fasttrack_impact: number;
  fasttrack_reliability: number;
  investigate_total: number;
};

export type TriageDistribution = {
  verdicts: Record<string, number>;
  mean_total: number;
  at_ceiling: number;
  at_ceiling_pct: number;
};

export type TriageSimulation = {
  events: number;
  settings?: { current: TriageSettings; proposed: TriageSettings };
  current: TriageDistribution | Record<string, never>;
  proposed: TriageDistribution | Record<string, never>;
};

export type EntityRow = {
  id: string;
  canonical_name: string;
  entity_type: "person" | "org" | "place" | "team" | "generic" | "unknown";
  aliases: string[];
  qid: string | null;
  qid_status: "pending" | "linked" | "no_match" | "unavailable";
  qid_confidence: number | null;
  /** The model's prose about its choice — the Q-number is the checked part. */
  qid_reason: string | null;
  confidence: number;
  review_status: "auto" | "needs_review" | "confirmed" | "rejected";
  /** Why it was queued, in Thai. Null when nobody needs to look. */
  risk: string | null;
  decided_by: string;
  mention_count: number;
  /** What the articles literally said — the only way to see a bad merge. */
  surface_forms: string[];
  first_seen: string | null;
  last_seen: string | null;
};

export type EntityCounts = {
  review: Record<string, number>;
  types: Record<string, number>;
  wikidata: Record<string, number>;
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
  simulateTriage: (k?: number) =>
    request<TriageSimulation>(
      `/triage/simulate${k === undefined ? "" : `?sensitivity_coefficient=${k}`}`,
    ),
  entities: (status?: string, limit = 100) =>
    request<EntityRow[]>(`/entities?limit=${limit}${status ? `&status=${status}` : ""}`),
  entityCounts: () => request<EntityCounts>("/entities/counts"),
  reviewEntity: (id: string, body: { decision: "confirmed" | "rejected"; canonical_name?: string }) =>
    request<EntityRow>(`/entities/${id}/review`, { method: "POST", body: JSON.stringify(body) }),
  rescoreTriage: () =>
    request<{ events: number; changed: number; settings: TriageSettings }>("/triage/rescore", {
      method: "POST",
    }),
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
