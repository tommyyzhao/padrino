// Bidirectional human play transport (Wave 9, US-153).
//
// The live client is the SSE-out half of the play spine: it consumes the
// live-tail Server-Sent Events stream (`?tail=true`, US-133) of released
// PUBLIC `public_event_v1` frames and reconnects BY SEQUENCE — the SSE `id:`
// field carries each event's sequence, so on a drop the client resumes from
// the last sequence it saw via the `?after=` query parameter, yielding no gaps
// and no duplicates. The POST-in half (action / chat) lives on `PadrinoClient`
// and is paired with idempotency keys so a network retry never double-acts.
//
// `EventSource` is injected so the transport is unit-testable without a real
// network: a fake EventSource drives `onopen` / `onmessage` / `onerror`
// synchronously.

import type { LiveEventFrame } from './types';

/** Minimal structural type of the browser `EventSource` we depend on. */
export interface LiveEventSource {
  readonly url: string;
  onopen: (() => void) | null;
  onmessage: ((event: { data: string; lastEventId?: string }) => void) | null;
  onerror: ((event: unknown) => void) | null;
  close(): void;
}

/** Factory that opens an SSE connection to a resume-aware URL. */
export type EventSourceFactory = (url: string) => LiveEventSource;
export type LiveConnectionState = 'live' | 'reconnecting' | 'offline';
export type ReconnectScheduler = (reconnect: () => void, delayMs: number) => void;

export interface LiveClientOptions {
  /** Builds the resume-aware live-tail URL for a sequence cursor (`after`). */
  buildUrl: (after: number | null) => string;
  /** Opens an SSE connection (defaults to the browser `EventSource`). */
  eventSourceFactory?: EventSourceFactory;
  /** Called for every released PUBLIC frame, in sequence order. */
  onFrame: (frame: LiveEventFrame) => void;
  /** Called when the stream errors; the client auto-reconnects by sequence. */
  onError?: (error: unknown) => void;
  /** Called when a stream opens successfully. */
  onOpen?: () => void;
  /** Called whenever the transport state changes. */
  onStateChange?: (state: LiveConnectionState) => void;
  /** Optional reconnect scheduler (defaults to `setTimeout(delayMs)`). */
  scheduleReconnect?: ReconnectScheduler;
  /** First reconnect delay after a drop. */
  reconnectBaseDelayMs?: number;
  /** Maximum reconnect delay after exponential growth and jitter. */
  reconnectMaxDelayMs?: number;
  /** Fractional positive jitter added to each reconnect delay. */
  reconnectJitterRatio?: number;
  /** Random source for reconnect jitter (defaults to `Math.random`). */
  random?: () => number;
}

const DEFAULT_RECONNECT_BASE_DELAY_MS = 500;
const DEFAULT_RECONNECT_MAX_DELAY_MS = 10_000;
const DEFAULT_RECONNECT_JITTER_RATIO = 0.25;

function defaultEventSourceFactory(url: string): LiveEventSource {
  // `EventSource` is a browser global; cast through the structural type so the
  // transport stays decoupled from the DOM lib in unit tests.
  return new EventSource(url) as unknown as LiveEventSource;
}

/**
 * Tail a game's released PUBLIC event stream with reconnect-by-sequence.
 *
 * The client tracks the highest sequence it has delivered. Each (re)connection
 * opens the live-tail URL with `after=<lastSequence>` so the server replays
 * only newer frames; a frame whose sequence is not strictly greater than the
 * last delivered one is dropped, making reconnect idempotent (no duplicates).
 */
export class LiveClient {
  private readonly opts: LiveClientOptions;
  private readonly factory: EventSourceFactory;
  private source: LiveEventSource | null = null;
  private lastSequence: number | null = null;
  private closed = false;
  private connectionState: LiveConnectionState = 'offline';
  private reconnectAttempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(opts: LiveClientOptions, lastSequence: number | null = null) {
    this.opts = opts;
    this.factory = opts.eventSourceFactory ?? defaultEventSourceFactory;
    this.lastSequence = lastSequence;
  }

  /** The highest sequence delivered so far (the resume cursor). */
  get cursor(): number | null {
    return this.lastSequence;
  }

  /** Current transport state for UI indicators. */
  get state(): LiveConnectionState {
    return this.connectionState;
  }

  /** Open the stream from the current cursor. */
  start(): void {
    this.closed = false;
    this.reconnectAttempt = 0;
    this.setState('reconnecting');
    this.connect();
  }

  /** Close the stream and stop reconnecting. */
  close(): void {
    this.closed = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.source?.close();
    this.source = null;
    this.setState('offline');
  }

  private connect(): void {
    if (this.closed) return;
    const url = this.opts.buildUrl(this.lastSequence);
    const source = this.factory(url);
    this.source = source;
    source.onopen = () => this.handleOpen(source);
    source.onmessage = (event) => this.handleMessage(event);
    source.onerror = (error) => this.handleError(source, error);
  }

  private handleOpen(source: LiveEventSource): void {
    if (this.closed || this.source !== source) return;
    this.opts.onOpen?.();
    this.setState('live');
  }

  private handleMessage(event: { data: string; lastEventId?: string }): void {
    if (this.closed) return;
    let frame: LiveEventFrame;
    try {
      frame = JSON.parse(event.data) as LiveEventFrame;
    } catch {
      // A keep-alive heartbeat is an SSE comment line, not a `data:` message,
      // so it never reaches here; a non-JSON data line is ignored defensively.
      return;
    }
    if (this.lastSequence !== null && frame.sequence <= this.lastSequence) {
      // Resume overlap after a reconnect: drop already-delivered frames so the
      // released stream is gap-free AND duplicate-free.
      return;
    }
    this.lastSequence = frame.sequence;
    this.reconnectAttempt = 0;
    this.opts.onFrame(frame);
  }

  private handleError(source: LiveEventSource, error: unknown): void {
    if (this.closed || this.source !== source) return;
    this.opts.onError?.(error);
    source.close();
    this.source = null;
    if (this.closed) return;
    this.setState('reconnecting');
    const delayMs = this.nextReconnectDelayMs();
    const reconnect = () => {
      this.reconnectTimer = null;
      this.connect();
    };
    if (this.opts.scheduleReconnect) {
      this.opts.scheduleReconnect(reconnect, delayMs);
    } else {
      this.reconnectTimer = setTimeout(reconnect, delayMs);
    }
  }

  private nextReconnectDelayMs(): number {
    const baseDelay = Math.max(1, this.opts.reconnectBaseDelayMs ?? DEFAULT_RECONNECT_BASE_DELAY_MS);
    const maxDelay = Math.max(
      baseDelay,
      this.opts.reconnectMaxDelayMs ?? DEFAULT_RECONNECT_MAX_DELAY_MS
    );
    const jitterRatio = Math.max(
      0,
      this.opts.reconnectJitterRatio ?? DEFAULT_RECONNECT_JITTER_RATIO
    );
    const random = this.opts.random ?? Math.random;
    const exponential = Math.min(maxDelay, baseDelay * 2 ** this.reconnectAttempt);
    this.reconnectAttempt += 1;
    const jitter = exponential * jitterRatio * Math.min(1, Math.max(0, random()));
    return Math.max(1, Math.min(maxDelay, Math.round(exponential + jitter)));
  }

  private setState(state: LiveConnectionState): void {
    if (this.connectionState === state) return;
    this.connectionState = state;
    this.opts.onStateChange?.(state);
  }
}

let _idempotencyCounter = 0;

/**
 * Generate a fresh idempotency key for an action / chat POST. Prefers
 * `crypto.randomUUID` when available; falls back to a monotonic
 * timestamp+counter token so a retry of the SAME submission reuses the key the
 * caller captured, while a NEW submission gets a distinct one.
 */
export function newIdempotencyKey(): string {
  const cryptoObj = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (cryptoObj?.randomUUID) {
    return cryptoObj.randomUUID();
  }
  _idempotencyCounter += 1;
  return `idem-${Date.now()}-${_idempotencyCounter}`;
}
