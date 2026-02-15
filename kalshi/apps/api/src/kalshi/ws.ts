/**
 * Kalshi WebSocket client with:
 *  - Authenticated connection (RSA-PSS headers)
 *  - Exponential backoff reconnection
 *  - Channel subscription management
 *  - Runtime message validation
 *  - Event emitter for downstream consumers
 */

import WebSocket from "ws";
import { EventEmitter } from "node:events";
import { z } from "zod";
import { kalshiConfig } from "../config.js";
import { createWsAuthHeaders, isAuthAvailable } from "./auth.js";
import { logger } from "../logger.js";

// ─── WS message schemas (runtime validated) ──────────────────

const WsTradeMessageSchema = z.object({
  type: z.literal("trade"),
  msg: z.object({
    trade_id: z.string().optional(),
    market_ticker: z.string().optional(),
    ticker: z.string().optional(),
    yes_price: z.number().optional(),
    no_price: z.number().optional(),
    count: z.number().optional(),
    taker_side: z.enum(["yes", "no"]).optional(),
    created_time: z.string().optional(),
    ts: z.number().optional(),
  }).passthrough(),
  sid: z.number().optional(),
});

const WsTickerMessageSchema = z.object({
  type: z.literal("ticker"),
  msg: z.object({
    market_ticker: z.string().optional(),
    ticker: z.string().optional(),
    yes_bid: z.number().optional(),
    yes_ask: z.number().optional(),
    no_bid: z.number().optional(),
    no_ask: z.number().optional(),
    last_price: z.number().optional(),
    volume: z.number().optional(),
    open_interest: z.number().optional(),
  }).passthrough(),
  sid: z.number().optional(),
});

const WsOrderbookSnapshotSchema = z.object({
  type: z.literal("orderbook_snapshot"),
  msg: z.object({
    market_ticker: z.string().optional(),
    yes: z.array(z.tuple([z.number(), z.number()])).optional(),
    no: z.array(z.tuple([z.number(), z.number()])).optional(),
  }).passthrough(),
  sid: z.number().optional(),
});

const WsOrderbookDeltaSchema = z.object({
  type: z.literal("orderbook_delta"),
  msg: z.object({
    market_ticker: z.string().optional(),
    price: z.number().optional(),
    delta: z.number().optional(),
    side: z.enum(["yes", "no"]).optional(),
  }).passthrough(),
  sid: z.number().optional(),
});

const WsSubscribedSchema = z.object({
  type: z.literal("subscribed"),
  msg: z.any(),
  id: z.number().optional(),
});

const WsErrorSchema = z.object({
  type: z.literal("error"),
  msg: z.object({
    code: z.number().optional(),
    msg: z.string().optional(),
  }).passthrough(),
  id: z.number().optional(),
});

// ─── Events ──────────────────────────────────────────────────

export interface KalshiWsEvents {
  trade: (data: z.infer<typeof WsTradeMessageSchema>["msg"]) => void;
  ticker: (data: z.infer<typeof WsTickerMessageSchema>["msg"]) => void;
  orderbook_snapshot: (data: z.infer<typeof WsOrderbookSnapshotSchema>["msg"]) => void;
  orderbook_delta: (data: z.infer<typeof WsOrderbookDeltaSchema>["msg"]) => void;
  connected: () => void;
  disconnected: (code: number, reason: string) => void;
  error: (err: Error) => void;
}

// ─── Client ──────────────────────────────────────────────────

export class KalshiWsClient extends EventEmitter {
  private ws: WebSocket | null = null;
  private messageId = 1;
  private reconnectAttempts = 0;
  private readonly maxReconnectDelay = 60_000;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private isClosing = false;
  private subscriptions: Array<{ channels: string[]; market_tickers?: string[] }> = [];
  private _reconnectCount = 0;
  private _messagesReceived = 0;

  get stats() {
    return {
      reconnects: this._reconnectCount,
      messagesReceived: this._messagesReceived,
      connected: this.ws?.readyState === WebSocket.OPEN,
    };
  }

  async connect(): Promise<void> {
    if (this.ws?.readyState === WebSocket.OPEN) return;
    this.isClosing = false;

    const headers: Record<string, string> = {};

    if (isAuthAvailable()) {
      Object.assign(headers, createWsAuthHeaders());
      logger.info("Connecting to Kalshi WS (authenticated)");
    } else {
      logger.info("Connecting to Kalshi WS (unauthenticated -- public channels only)");
    }

    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(kalshiConfig.wsUrl, { headers });

      this.ws.on("open", () => {
        logger.info("Kalshi WS connected");
        this.reconnectAttempts = 0;
        this.emit("connected");
        this.resubscribe();
        resolve();
      });

      this.ws.on("message", (data) => {
        this._messagesReceived++;
        this.handleMessage(data.toString());
      });

      this.ws.on("close", (code, reason) => {
        const reasonStr = reason?.toString() ?? "";
        logger.warn({ code, reason: reasonStr }, "Kalshi WS disconnected");
        this.emit("disconnected", code, reasonStr);
        if (!this.isClosing) {
          this.scheduleReconnect();
        }
      });

      this.ws.on("error", (err) => {
        logger.error({ err }, "Kalshi WS error");
        // Do NOT this.emit("error", err) — Node's EventEmitter treats
        // unhandled "error" events as fatal.  The promise rejection
        // (below) is sufficient; reconnect is driven by the "close" event.
        reject(err);
      });

      this.ws.on("ping", (data) => {
        this.ws?.pong(data);
      });
    });
  }

  disconnect(): void {
    this.isClosing = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.close(1000, "Client closing");
      this.ws = null;
    }
  }

  // ─── Subscribe ───────────────────────────────────────────

  subscribe(channels: string[], marketTickers?: string[]): void {
    // Track for resubscribe on reconnect
    this.subscriptions.push({ channels, market_tickers: marketTickers });

    if (this.ws?.readyState !== WebSocket.OPEN) {
      logger.debug({ channels, marketTickers }, "WS not open, subscription queued");
      return;
    }

    this.sendSubscription(channels, marketTickers);
  }

  private sendSubscription(channels: string[], marketTickers?: string[]): void {
    const msg: Record<string, unknown> = {
      id: this.messageId++,
      cmd: "subscribe",
      params: {
        channels,
      },
    };

    if (marketTickers && marketTickers.length > 0) {
      (msg.params as Record<string, unknown>).market_tickers = marketTickers;
    }

    this.send(msg);
    logger.info({ channels, marketTickers: marketTickers?.length ?? "all" }, "Subscribed to channels");
  }

  private resubscribe(): void {
    for (const sub of this.subscriptions) {
      this.sendSubscription(sub.channels, sub.market_tickers);
    }
  }

  private send(data: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }

  // ─── Message handling ────────────────────────────────────

  private handleMessage(raw: string): void {
    let data: unknown;
    try {
      data = JSON.parse(raw);
    } catch {
      logger.warn({ raw: raw.slice(0, 200) }, "Non-JSON WS message");
      return;
    }

    const obj = data as Record<string, unknown>;
    const type = obj.type as string;

    switch (type) {
      case "trade": {
        const parsed = WsTradeMessageSchema.safeParse(data);
        if (parsed.success) {
          this.emit("trade", parsed.data.msg);
        } else {
          logger.debug({ errors: parsed.error.issues, raw: raw.slice(0, 500) }, "Trade message validation failed");
          // Still emit with raw msg for resilience
          this.emit("trade", obj.msg);
        }
        break;
      }

      case "ticker": {
        const parsed = WsTickerMessageSchema.safeParse(data);
        if (parsed.success) {
          this.emit("ticker", parsed.data.msg);
        } else {
          this.emit("ticker", obj.msg);
        }
        break;
      }

      case "orderbook_snapshot": {
        const parsed = WsOrderbookSnapshotSchema.safeParse(data);
        if (parsed.success) {
          this.emit("orderbook_snapshot", parsed.data.msg);
        } else {
          this.emit("orderbook_snapshot", obj.msg);
        }
        break;
      }

      case "orderbook_delta": {
        const parsed = WsOrderbookDeltaSchema.safeParse(data);
        if (parsed.success) {
          this.emit("orderbook_delta", parsed.data.msg);
        } else {
          this.emit("orderbook_delta", obj.msg);
        }
        break;
      }

      case "subscribed": {
        const parsed = WsSubscribedSchema.safeParse(data);
        logger.debug({ msg: parsed.success ? parsed.data : obj }, "WS subscribed confirmation");
        break;
      }

      case "error": {
        const parsed = WsErrorSchema.safeParse(data);
        const errMsg = parsed.success ? parsed.data.msg : obj.msg;
        logger.error({ msg: errMsg }, "Kalshi WS error message");
        break;
      }

      default:
        logger.debug({ type, keys: Object.keys(obj) }, "Unknown WS message type");
    }
  }

  // ─── Reconnection ────────────────────────────────────────

  private scheduleReconnect(): void {
    // Guard: only one reconnect timer at a time.
    // Without this, both the "close" handler AND the promise rejection
    // in catch{} would each schedule their own timer — doubling every
    // failure cycle and causing an exponential reconnect storm.
    if (this.isClosing || this.reconnectTimer) return;

    // Exponential backoff: 1s, 2s, 4s, 8s, ... up to 60s
    const delay = Math.min(
      this.maxReconnectDelay,
      1000 * Math.pow(2, this.reconnectAttempts),
    );
    this.reconnectAttempts++;
    this._reconnectCount++;

    logger.info({ delay, attempt: this.reconnectAttempts }, "Scheduling WS reconnect");

    this.reconnectTimer = setTimeout(async () => {
      this.reconnectTimer = null; // Clear before attempting so guard resets
      try {
        await this.connect();
      } catch (err) {
        logger.error({ err }, "Reconnect failed");
        if (!this.isClosing) {
          this.scheduleReconnect();
        }
      }
    }, delay);
  }
}
