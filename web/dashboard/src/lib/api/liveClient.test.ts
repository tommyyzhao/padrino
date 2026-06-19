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
  onmessage: ((event: { data: string; lastEventId?: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
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

  it('reconnects by sequence and drops resume-overlap duplicates', () => {
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
    expect(frames).toEqual([1, 2, 3]);
    expect(client.cursor).toBe(3);
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
