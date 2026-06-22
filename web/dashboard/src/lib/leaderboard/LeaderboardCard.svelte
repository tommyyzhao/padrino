<script lang="ts">
  import type { PublicRatingCardResponse } from '$lib/api/types';
  import Card from '$lib/components/Card.svelte';

  interface Props {
    card: PublicRatingCardResponse;
    subsection: string;
  }

  let { card, subsection }: Props = $props();

  function scoreText(card: PublicRatingCardResponse): string {
    if (card.metric === 'solo_success_rate') {
      const value = card.mean_success_rate ?? card.score;
      return `${(value * 100).toFixed(0)}%`;
    }
    return (card.conservative_score ?? card.score).toFixed(1);
  }

  function uncertaintyText(card: PublicRatingCardResponse): string {
    if (
      card.metric === 'solo_success_rate' &&
      card.credible_interval_low !== null &&
      card.credible_interval_high !== null
    ) {
      return `CI ${(card.credible_interval_low * 100).toFixed(0)}-${(card.credible_interval_high * 100).toFixed(0)}%`;
    }
    if (card.sigma !== null) {
      return `±${card.sigma.toFixed(1)}`;
    }
    return '';
  }

  function rankText(card: PublicRatingCardResponse): string {
    if (card.provisional) return 'Provisional';
    if (card.rank !== null) return `#${card.rank}`;
    return 'Unranked';
  }

  function sampleLabel(card: PublicRatingCardResponse): string {
    return card.metric === 'solo_success_rate' ? 'Successes' : 'Games';
  }

  function sampleText(card: PublicRatingCardResponse): string {
    if (card.metric === 'solo_success_rate') {
      return `${card.successes ?? 0}/${card.attempts ?? 0}`;
    }
    return `${card.games ?? 0}`;
  }

  function labelText(value: string): string {
    const normalized = value.toUpperCase();
    if (normalized === 'GLOBAL') return 'Global';
    if (normalized === 'TOWN') return 'Town';
    if (normalized === 'MAFIA') return 'Scum';
    return value
      .split('_')
      .filter(Boolean)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase())
      .join(' ');
  }

  function scopeText(card: PublicRatingCardResponse): string {
    if (card.scope_type === 'GLOBAL') return 'Global';
    if (card.scope_type === 'FACTION') return `Faction: ${labelText(card.scope_value)}`;
    return `${labelText(card.scope_type)}: ${labelText(card.scope_value)}`;
  }
</script>

<Card
  class="space-y-4"
  data-testid="leaderboard-card"
  data-section={card.section}
  data-context={card.context_kind}
  data-subsection={subsection}
  data-scope-type={card.scope_type}
  data-scope-value={card.scope_value}
>
  <div class="flex items-start justify-between gap-3">
    <div class="min-w-0">
      <p class="text-xs uppercase tracking-wider text-muted-foreground">
        {card.context_label}
      </p>
      <h3 class="truncate text-base font-semibold" data-testid="leaderboard-card-name">
        {card.display_name}
      </h3>
      <p class="mt-1 text-xs text-muted-foreground" data-testid="leaderboard-card-scope">
        {scopeText(card)}
      </p>
    </div>
    <span class="shrink-0 text-sm font-medium" data-testid="leaderboard-card-rank">
      {rankText(card)}
    </span>
  </div>
  <div class="flex items-end justify-between gap-3">
    <div>
      <p class="text-xs text-muted-foreground">{card.metric_label}</p>
      <p class="text-3xl font-semibold">{scoreText(card)}</p>
    </div>
    <p class="text-sm text-muted-foreground">{uncertaintyText(card)}</p>
  </div>
  <div class="flex items-center justify-between text-sm">
    <span class="text-muted-foreground">{sampleLabel(card)}</span>
    <span>{sampleText(card)}</span>
  </div>
  {#if card.provisional}
    <p class="text-xs text-muted-foreground" data-testid="leaderboard-card-provisional">
      <span class="font-medium">Under-sampled</span>{#if card.provisional_reason}: {card.provisional_reason}{/if}
    </p>
  {:else if card.provisional_reason}
    <p class="text-xs text-muted-foreground" data-testid="leaderboard-card-provisional">
      {card.provisional_reason}
    </p>
  {/if}
</Card>
