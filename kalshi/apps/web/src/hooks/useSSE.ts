"use client";

import { useEffect, useRef, useCallback, useState } from "react";

interface UseSSEOptions {
  url: string;
  onAlert?: (data: Record<string, unknown>) => void;
  onTicker?: (data: Record<string, unknown>) => void;
  onTrade?: (data: Record<string, unknown>) => void;
  enabled?: boolean;
}

export function useSSE({ url, onAlert, onTicker, onTrade, enabled = true }: UseSSEOptions) {
  const [connected, setConnected] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();

  const connect = useCallback(() => {
    if (!enabled) return;

    const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";
    const es = new EventSource(`${basePath}${url}`);
    esRef.current = es;

    es.addEventListener("connected", () => {
      setConnected(true);
    });

    es.addEventListener("alert", (e) => {
      try {
        const data = JSON.parse(e.data);
        onAlert?.(data);
      } catch {}
    });

    es.addEventListener("ticker", (e) => {
      try {
        const data = JSON.parse(e.data);
        onTicker?.(data);
      } catch {}
    });

    es.addEventListener("trade", (e) => {
      try {
        const data = JSON.parse(e.data);
        onTrade?.(data);
      } catch {}
    });

    es.onerror = () => {
      setConnected(false);
      es.close();
      // Reconnect after 3s
      reconnectTimer.current = setTimeout(connect, 3000);
    };
  }, [url, onAlert, onTicker, onTrade, enabled]);

  useEffect(() => {
    connect();
    return () => {
      esRef.current?.close();
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    };
  }, [connect]);

  return { connected };
}
