// Play-session store (Wave 9, US-153) — Svelte 5 runes.
//
// Wires the SSE-out live client to the pure play-state reducer: the store holds
// the derived board / votes / chat as `$state` and folds every RELEASED frame
// from `LiveClient` into it (the buffered hold + symmetric release delay live
// server-side, so a frame reaching the client is already released). Board /
// votes / chat are exposed as `$derived` reads. The reducer is pure and unit
// tested separately; this file is the thin reactive shell.

import { LiveClient, type EventSourceFactory } from './api/liveClient';
import type { PadrinoClient } from './api/client';
import {
  applyFrame,
  emptyPlayState,
  type BoardSeat,
  type PlayState,
  type ReleasedChatLine
} from './api/playState';
import type { LiveEventFrame } from './api/types';

export interface PlaySessionOptions {
  client: PadrinoClient;
  gameId: string;
  /** Injectable EventSource factory (tests pass a fake). */
  eventSourceFactory?: EventSourceFactory;
}

export function createPlaySession(opts: PlaySessionOptions) {
  let state = $state<PlayState>(emptyPlayState());

  const live = new LiveClient({
    buildUrl: (after) => opts.client.liveTailUrl(opts.gameId, after),
    eventSourceFactory: opts.eventSourceFactory,
    onFrame: (frame: LiveEventFrame) => {
      // The reducer ignores resume-overlap frames, so reconnect is idempotent.
      state = applyFrame(state, frame);
    }
  });

  function start(): void {
    live.start();
  }

  function close(): void {
    live.close();
  }

  return {
    start,
    close,
    get phase(): string {
      return state.phase;
    },
    get seats(): BoardSeat[] {
      return state.seats;
    },
    get votes(): Record<string, string | null> {
      return state.votes;
    },
    get chat(): ReleasedChatLine[] {
      return state.chat;
    },
    get winner(): string | null {
      return state.winner;
    },
    get terminal(): boolean {
      return state.terminal;
    },
    get lastSequence(): number | null {
      return state.lastSequence;
    }
  };
}

export type PlaySession = ReturnType<typeof createPlaySession>;
