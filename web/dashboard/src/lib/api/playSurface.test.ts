import { describe, expect, it } from 'vitest';
import {
  actionAllowed,
  composerStatusFromChat,
  countdownBucket,
  countdownLabel,
  isNightActionPhase,
  isVotePhase,
  secondsUntil,
  spriteUrl
} from './playSurface';
import type { LegalActionsView } from './types';

describe('composerStatusFromChat', () => {
  it('maps backend chat statuses to the composer lifecycle', () => {
    expect(composerStatusFromChat('HELD')).toBe('pending');
    expect(composerStatusFromChat('RELEASED')).toBe('released');
    expect(composerStatusFromChat('BLOCKED')).toBe('blocked');
    // An unknown status is treated conservatively as still in the hold.
    expect(composerStatusFromChat('SOMETHING_NEW')).toBe('pending');
  });
});

describe('countdown bucketing (non-precise, no timing leak)', () => {
  it('buckets seconds remaining into coarse labels, never the exact seconds', () => {
    expect(countdownBucket(null)).toBe('none');
    expect(countdownBucket(0)).toBe('none');
    expect(countdownBucket(5)).toBe('ending');
    expect(countdownBucket(14)).toBe('ending');
    expect(countdownBucket(30)).toBe('soon');
    expect(countdownBucket(59)).toBe('soon');
    expect(countdownBucket(120)).toBe('plenty');
  });

  it('renders a non-precise label for each bucket', () => {
    expect(countdownLabel('ending')).toBe('Ending soon');
    expect(countdownLabel('soon')).toBe('A little time left');
    expect(countdownLabel('plenty')).toBe('Plenty of time');
    expect(countdownLabel('none')).toBe('No timer');
  });

  it('computes seconds until an ISO deadline relative to an injected clock', () => {
    const now = Date.parse('2026-06-19T00:00:00Z');
    expect(secondsUntil('2026-06-19T00:01:00Z', now)).toBe(60);
    expect(secondsUntil('2026-06-19T00:00:00Z', now)).toBeNull();
    expect(secondsUntil(null, now)).toBeNull();
    expect(secondsUntil('not-a-date', now)).toBeNull();
  });
});

describe('legal-action gating', () => {
  const vote: LegalActionsView = { allowed_action_types: ['VOTE', 'ABSTAIN'], legal_targets: ['p2'] };
  const night: LegalActionsView = { allowed_action_types: ['INVESTIGATE'], legal_targets: ['p3'] };
  const noop: LegalActionsView = { allowed_action_types: ['NOOP'], legal_targets: [] };

  it('reports whether an action type is allowed', () => {
    expect(actionAllowed(vote, 'VOTE')).toBe(true);
    expect(actionAllowed(vote, 'INVESTIGATE')).toBe(false);
    expect(actionAllowed(null, 'VOTE')).toBe(false);
  });

  it('classifies vote vs night-action phases', () => {
    expect(isVotePhase(vote)).toBe(true);
    expect(isVotePhase(night)).toBe(false);
    expect(isNightActionPhase(night)).toBe(true);
    expect(isNightActionPhase(vote)).toBe(false);
    expect(isNightActionPhase(noop)).toBe(false);
  });
});

describe('spriteUrl', () => {
  it('builds the immutable static asset URL for a theme pack + key', () => {
    expect(spriteUrl('http://localhost:8000', 'pixel_town', 'archetype_a')).toBe(
      'http://localhost:8000/public/sprites/pixel_town/archetype_a'
    );
  });

  it('falls back to the placeholder for a missing pack/key', () => {
    expect(spriteUrl('http://localhost:8000/', null, 'archetype_a')).toBe(
      'http://localhost:8000/public/sprites/placeholder'
    );
    expect(spriteUrl('http://localhost:8000', 'pixel_town', null)).toBe(
      'http://localhost:8000/public/sprites/placeholder'
    );
    expect(spriteUrl('http://localhost:8000', 'pixel_town', 'placeholder')).toBe(
      'http://localhost:8000/public/sprites/placeholder'
    );
  });
});
