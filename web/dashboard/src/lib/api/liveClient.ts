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
// network: a fake EventSource drives `onmessage` / `onerror` synchronously.

import type { LiveEventFrame } from './types';

/** Minimal structural type of the browser `EventSource` we depend on. */
export interface LiveEventSource {
  readonly url: string;
  onmessage: ((event: { data: string; lastEventId?: string }) => void) | null;
  onerror: ((event: unknown) => void) | null;
  close(): void;
}

/** Factory that opens an SSE connection to a resume-aware URL. */
export type EventSourceFactory = (url: string) => LiveEventSource;

export interface LiveClientOptions {
  /** Builds the resume-aware live-tail URL for a sequence cursor (`after`). */
  buildUrl: (after: number | null) => string;
  /** Opens an SSE connection (defaults to the browser `EventSource`). */
  eventSourceFactory?: EventSourceFactory;
  /** Called for every released PUBLIC frame, in sequence order. */
  onFrame: (frame: LiveEventFrame) => void;
  /** Called when the stream errors; the client auto-reconnects by sequence. */
  onError?: (error: unknown) => void;
  /** Optional reconnect scheduler (defaults to `setTimeout`, 0ms). */
  scheduleReconnect?: (reconnect: () => void) => void;
}

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

  constructor(opts: LiveClientOptions, lastSequence: number | null = null) {
    this.opts = opts;
    this.factory = opts.eventSourceFactory ?? defaultEventSourceFactory;
    this.lastSequence = lastSequence;
  }

  /** The highest sequence delivered so far (the resume cursor). */
  get cursor(): number | null {
    return this.lastSequence;
  }

  /** Open the stream from the current cursor. */
  start(): void {
    this.closed = false;
    this.connect();
  }

  /** Close the stream and stop reconnecting. */
  close(): void {
    this.closed = true;
    this.source?.close();
    this.source = null;
  }

  private connect(): void {
    if (this.closed) return;
    const url = this.opts.buildUrl(this.lastSequence);
    const source = this.factory(url);
    this.source = source;
    source.onmessage = (event) => this.handleMessage(event);
    source.onerror = (error) => this.handleError(error);
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
    this.opts.onFrame(frame);
  }

  private handleError(error: unknown): void {
    this.opts.onError?.(error);
    this.source?.close();
    this.source = null;
    if (this.closed) return;
    const reconnect = () => this.connect();
    if (this.opts.scheduleReconnect) {
      this.opts.scheduleReconnect(reconnect);
    } else {
      setTimeout(reconnect, 0);
    }
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
