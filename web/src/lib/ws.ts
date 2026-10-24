import { api } from "./api";
import { backoffMs } from "./retry";
import type { StreamMessage } from "./types";

type Handler = (msg: StreamMessage) => void;
export type ConnState = "idle" | "connecting" | "open" | "reconnecting";

interface Sub {
  token: string;
  lastId: string;
  handlers: Set<Handler>;
}

function idGreater(a: string, b: string): boolean {
  const [am = "0", as = "0"] = a.split("-");
  const [bm = "0", bs = "0"] = b.split("-");
  const d = Number(am) - Number(bm);
  return d !== 0 ? d > 0 : Number(as) > Number(bs);
}

class Realtime {
  private ws: WebSocket | null = null;
  private subs = new Map<string, Sub>();
  private stateListeners = new Set<(s: ConnState) => void>();
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private lastPong = 0;
  state: ConnState = "idle";

  onState(fn: (s: ConnState) => void): () => void {
    this.stateListeners.add(fn);
    fn(this.state);
    return () => this.stateListeners.delete(fn);
  }

  private setState(s: ConnState): void {
    this.state = s;
    for (const fn of this.stateListeners) fn(s);
  }

  subscribe(topic: string, token: string, since: string, handler: Handler): () => void {
    let sub = this.subs.get(topic);
    if (!sub) {
      sub = { token, lastId: since || "0-0", handlers: new Set() };
      this.subs.set(topic, sub);
      this.sendSub(topic, sub);
    } else {
      sub.token = token;
    }
    sub.handlers.add(handler);
    this.ensureConnected();
    return () => {
      const s = this.subs.get(topic);
      if (!s) return;
      s.handlers.delete(handler);
      if (s.handlers.size === 0) {
        this.subs.delete(topic);
        this.send({ op: "unsub", topic });
        if (this.subs.size === 0) this.close();
      }
    };
  }

  control(topic: string, cmd: Record<string, unknown>, timeoutMs = 8000): Promise<{ ok: boolean; error?: string }> {
    const req = crypto.randomUUID().slice(0, 8);
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        off();
        resolve({ ok: false, error: "No acknowledgement from the simulation yet" });
      }, timeoutMs);
      const sub = this.subs.get(topic);
      const handler: Handler = (m) => {
        const ev = m.event as { type: string; req?: string; ok?: boolean; error?: string };
        if (ev.type === "ack" && ev.req === req) {
          clearTimeout(timer);
          off();
          resolve({ ok: !!ev.ok, error: ev.error ?? undefined });
        }
      };
      const off = () => sub?.handlers.delete(handler);
      sub?.handlers.add(handler);
      this.send({ op: "ctl", topic, req, cmd });
    });
  }

  private sendSub(topic: string, sub: Sub): void {
    this.send({ op: "sub", topic, token: sub.token, since: sub.lastId });
  }

  private send(obj: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  private ensureConnected(): void {
    if (this.ws || this.reconnectTimer) return;
    void this.connect();
  }

  private async connect(): Promise<void> {
    this.setState(this.attempt === 0 ? "connecting" : "reconnecting");
    let ticket: string;
    try {
      ({ ticket } = await api.post<{ ticket: string }>("/api/v1/realtime/tickets"));
    } catch {
      this.scheduleReconnect();
      return;
    }
    if (this.subs.size === 0) {
      this.setState("idle");
      return;
    }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws?ticket=${encodeURIComponent(ticket)}`);
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.lastPong = Date.now();
      this.setState("open");
      for (const [topic, sub] of this.subs) this.sendSub(topic, sub);
      this.heartbeat = setInterval(() => {
        if (Date.now() - this.lastPong > 45_000) {
          ws.close(4000, "heartbeat timeout");
          return;
        }
        this.send({ op: "ping" });
      }, 15_000);
    };
    ws.onmessage = (e) => {
      let msg: StreamMessage | { type?: string };
      try {
        msg = JSON.parse(e.data as string);
      } catch {
        return;
      }
      if ("topic" in msg && "id" in msg && "event" in msg) {
        const sub = this.subs.get(msg.topic);
        if (!sub) return;
        if (!idGreater(msg.id, sub.lastId)) return;
        sub.lastId = msg.id;
        for (const h of [...sub.handlers]) h(msg);
        return;
      }
      if ((msg as { type?: string }).type === "pong") this.lastPong = Date.now();
    };
    ws.onclose = () => {
      if (this.heartbeat) clearInterval(this.heartbeat);
      this.heartbeat = null;
      this.ws = null;
      if (this.subs.size > 0) this.scheduleReconnect();
      else this.setState("idle");
    };
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    this.setState("reconnecting");
    const delay = 250 + backoffMs(this.attempt++, 500, 10_000);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, delay);
  }

  private close(): void {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.ws?.close(1000, "no subscriptions");
    this.ws = null;
    this.attempt = 0;
    this.setState("idle");
  }
}

export const realtime = new Realtime();
