const API_BASE = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

async function fetchApi<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${await res.text()}`);
  }
  return res.json() as Promise<T>;
}

// ─── Alerts ──────────────────────────────────────────────────

export interface AlertRow {
  id: string;
  market_ticker: string;
  alert_type: string;
  anomaly_score: number;
  trade_id: string | null;
  explanation: Record<string, unknown>;
  reason: string;
  created_at: string;
  market_title: string | null;
  close_time: string | null;
  last_price_cents: number | null;
  exchange: string;
}

export async function getAlerts(params?: {
  min_score?: number;
  alert_type?: string;
  exchange?: string;
  limit?: number;
  offset?: number;
}): Promise<{ alerts: AlertRow[] }> {
  const qs = new URLSearchParams();
  if (params?.min_score) qs.set("min_score", params.min_score.toString());
  if (params?.alert_type) qs.set("alert_type", params.alert_type);
  if (params?.exchange) qs.set("exchange", params.exchange);
  if (params?.limit) qs.set("limit", params.limit.toString());
  if (params?.offset) qs.set("offset", params.offset.toString());

  return fetchApi(`/api/alerts?${qs.toString()}`);
}

// ─── Markets ─────────────────────────────────────────────────

export interface MarketRow {
  ticker: string;
  event_ticker: string;
  title: string;
  status: string;
  close_time: string | null;
  last_price_cents: number | null;
  yes_bid_cents: number | null;
  yes_ask_cents: number | null;
  volume: number | null;
  open_interest: number | null;
  exchange: string;
}

export async function getMarkets(params?: {
  search?: string;
  exchange?: string;
  limit?: number;
}): Promise<{ markets: MarketRow[] }> {
  const qs = new URLSearchParams();
  if (params?.search) qs.set("search", params.search);
  if (params?.exchange) qs.set("exchange", params.exchange);
  if (params?.limit) qs.set("limit", params.limit.toString());
  return fetchApi(`/api/markets?${qs.toString()}`);
}

export interface MarketDetail {
  market: MarketRow;
  trades: Array<{
    trade_id: string;
    market_ticker: string;
    yes_price_cents: number;
    no_price_cents: number;
    count: number;
    taker_side: string;
    created_time: string;
  }>;
  alerts: AlertRow[];
  book: {
    mid: number | null;
    best_yes_bid: number | null;
    best_yes_ask: number | null;
    yes_bids: [number, number][];
    no_bids: [number, number][];
  } | null;
}

export async function getMarketDetail(ticker: string): Promise<MarketDetail> {
  return fetchApi(`/api/markets/${ticker}`);
}

// ─── Config ──────────────────────────────────────────────────

export async function getConfig() {
  return fetchApi<{ config: Record<string, unknown>; presets: string[] }>("/api/config");
}

export async function updateConfig(body: Record<string, unknown>) {
  return fetchApi("/api/config", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

// ─── Stats ───────────────────────────────────────────────────

export async function getStats() {
  return fetchApi<Record<string, unknown>>("/api/config/stats");
}
