import { describe, expect, it, vi } from 'vitest';
import { LiveClient, newIdempotencyKey, type LiveEventSource } from './liveClient';
import type { LiveEventFrame } from './types';

function frame(sequence: number, overrides: Partial<LiveEventFrame> = {}): LiveEventFrame {
  return {
    schema_version: 'public_event_v1',
    sequence,
    phase: 'DAY_1',
    event_type: 'PublicMessageSubmitted',
    visibility: 'PUBLIC',
    actor_player_id: 'p1',
    payload: { text: `m${sequence}` },
    prev_event_hash: '',
    event_hash: '',
    ...overrides
  };
}

class FakeEventSource implements LiveEventSource {
  readonly url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string; lastEventId?: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
  }

  open(): void {
    this.onopen?.();
  }

  emit(f: LiveEventFrame): void {
    this.onmessage?.({ data: JSON.stringify(f), lastEventId: String(f.sequence) });
  }

  fail(): void {
    this.onerror?.(new Error('boom'));
  }

  close(): void {
    this.closed = true;
  }
}

describe('LiveClient', () => {
  it('emits connection-state transitions and an onOpen callback', () => {
    const sources: FakeEventSource[] = [];
    const states: string[] = [];
    let opens = 0;
    const pending: { reconnect: (() => void) | null } = { reconnect: null };
    const client = new LiveClient({
      buildUrl: () => 'http://api/live',
      eventSourceFactory: (url) => {
        const s = new FakeEventSource(url);
        sources.push(s);
        return s;
      },
      onFrame: () => {},
      onOpen: () => {
        opens += 1;
      },
      onStateChange: (state) => states.push(state),
      scheduleReconnect: (fn) => {
        pending.reconnect = fn;
      }
    });

    client.start();
    sources[0].open();
    sources[0].fail();
    pending.reconnect?.();
    sources[1].open();
    client.close();

    expect(opens).toBe(2);
    expect(states).toEqual(['reconnecting', 'live', 'reconnecting', 'live', 'offline']);
  });

  it('delivers released frames in order and tracks the resume cursor', () => {
    const sources: FakeEventSource[] = [];
    const frames: number[] = [];
    const client = new LiveClient({
      buildUrl: (after) => `http://api/live?after=${after ?? ''}`,
      eventSourceFactory: (url) => {
        const s = new FakeEventSource(url);
        sources.push(s);
        return s;
      },
      onFrame: (f) => frames.push(f.sequence)
    });
    client.start();
    sources[0].emit(frame(1));
    sources[0].emit(frame(2));
    expect(frames).toEqual([1, 2]);
    expect(client.cursor).toBe(2);
  });

  it('reconnects by sequence without frame duplication or loss', () => {
    const sources: FakeEventSource[] = [];
    const frames: number[] = [];
    const pending: { reconnect: (() => void) | null } = { reconnect: null };
    const client = new LiveClient({
      buildUrl: (after) => `http://api/live?after=${after ?? ''}`,
      eventSourceFactory: (url) => {
        const s = new FakeEventSource(url);
        sources.push(s);
        return s;
      },
      onFrame: (f) => frames.push(f.sequence),
      scheduleReconnect: (fn) => {
        pending.reconnect = fn;
      }
    });
    client.start();
    sources[0].emit(frame(1));
    sources[0].emit(frame(2));
    sources[0].fail();
    expect(sources[0].closed).toBe(true);
    expect(pending.reconnect).not.toBeNull();
    pending.reconnect?.();
    // The reconnect opens with the resume cursor.
    expect(sources[1].url).toContain('after=2');
    // The server re-sends an overlap frame (2) plus a new one (3); the overlap
    // is dropped so the released stream stays gap-free AND duplicate-free.
    sources[1].emit(frame(2));
    sources[1].emit(frame(3));
    sources[1].emit(frame(4));
    expect(frames).toEqual([1, 2, 3, 4]);
    expect(client.cursor).toBe(4);
  });

  it('uses increasing bounded reconnect backoff with jitter', () => {
    const sources: FakeEventSource[] = [];
    const delays: number[] = [];
    const pending: { reconnect: (() => void) | null } = { reconnect: null };
    const client = new LiveClient({
      buildUrl: () => 'http://api/live',
      eventSourceFactory: (url) => {
        const s = new FakeEventSource(url);
        sources.push(s);
        return s;
      },
      onFrame: () => {},
      reconnectBaseDelayMs: 100,
      reconnectMaxDelayMs: 500,
      reconnectJitterRatio: 0.5,
      random: () => 1,
      scheduleReconnect: (fn, delayMs) => {
        delays.push(delayMs);
        pending.reconnect = fn;
      }
    });

    client.start();
    sources[0].fail();
    pending.reconnect?.();
    sources[1].fail();
    pending.reconnect?.();
    sources[2].fail();
    pending.reconnect?.();
    sources[3].fail();

    expect(delays.length).toBe(4);
    expect(delays[0]).toBeGreaterThan(0);
    expect(delays[1]).toBeGreaterThan(delays[0]);
    expect(delays[2]).toBeGreaterThan(delays[1]);
    expect(delays.every((delay) => delay > 0 && delay <= 500)).toBe(true);
    expect(delays[3]).toBe(500);
  });

  it('stops reconnecting after close()', () => {
    const sources: FakeEventSource[] = [];
    const pending: { reconnect: (() => void) | null } = { reconnect: null };
    const client = new LiveClient({
      buildUrl: () => 'http://api/live',
      eventSourceFactory: (url) => {
        const s = new FakeEventSource(url);
        sources.push(s);
        return s;
      },
      onFrame: () => {},
      scheduleReconnect: (fn) => {
        pending.reconnect = fn;
      }
    });
    client.start();
    client.close();
    expect(sources[0].closed).toBe(true);
    sources[0].fail();
    // A close before the scheduled reconnect runs must not reopen the stream.
    pending.reconnect?.();
    expect(sources.length).toBe(1);
  });

  it('ignores non-JSON data lines defensively', () => {
    const sources: FakeEventSource[] = [];
    const onError = vi.fn();
    const client = new LiveClient({
      buildUrl: () => 'http://api/live',
      eventSourceFactory: (url) => {
        const s = new FakeEventSource(url);
        sources.push(s);
        return s;
      },
      onFrame: () => {
        throw new Error('should not be called');
      },
      onError
    });
    client.start();
    sources[0].onmessage?.({ data: 'not-json' });
    expect(onError).not.toHaveBeenCalled();
  });
});

describe('newIdempotencyKey', () => {
  it('produces distinct keys for distinct submissions', () => {
    const a = newIdempotencyKey();
    const b = newIdempotencyKey();
    expect(a).not.toBe(b);
    expect(a.length).toBeGreaterThan(0);
  });
});
