<script lang="ts">
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { GauntletReport } from '$lib/api/types';
  import { shortenHash } from '$lib/utils';

  let gauntletId = $derived($page.params.id);
  let report = $state<GauntletReport | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function load() {
    if (!gauntletId) return;
    loading = true;
    error = null;
    try {
      // Use the public (identity-redacted) endpoint by default. Operators
      // viewing identifying data can swap to padrino.client.getGauntletReport
      // — both endpoints return the same JSON shape modulo redaction.
      report = await padrino.client.publicGauntletReport(gauntletId);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  function pct(value: number): string {
    return `${(value * 100).toFixed(1)}%`;
  }

  function ciLabel(point: number, lower: number, upper: number): string {
    return `${pct(point)} (95% CI ${pct(lower)} – ${pct(upper)})`;
  }

  function sign(value: number): string {
    if (value > 0) return `+${value.toFixed(2)}`;
    return value.toFixed(2);
  }

  onMount(load);
</script>

<div class="mb-4">
  <a class="text-sm underline" href="/games">← Games</a>
</div>

<h1 class="mb-2 text-xl font-semibold" data-testid="gauntlet-report-title">Gauntlet Report</h1>
<p class="mb-4 font-mono text-xs text-muted-foreground" data-testid="gauntlet-report-id">
  {gauntletId}
</p>

{#if loading}
  <p data-testid="gauntlet-report-loading">Loading…</p>
{:else if error}
  <p class="text-sm text-red-500" data-testid="gauntlet-report-error">{error}</p>
{:else if report === null}
  <p class="text-sm text-muted-foreground" data-testid="gauntlet-report-empty">
    No report available for this gauntlet.
  </p>
{:else}
  <div
    class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3"
    data-testid="gauntlet-report-shell"
    data-status={report.status}
  >
    <Card>
      <h2 class="mb-2 text-sm font-semibold">Summary</h2>
      <dl class="grid grid-cols-2 gap-2 text-xs" data-testid="gauntlet-report-summary">
        <dt class="text-muted-foreground">Status</dt>
        <dd data-testid="gauntlet-report-status">{report.status}</dd>
        <dt class="text-muted-foreground">Ruleset</dt>
        <dd>{report.ruleset_id}</dd>
        <dt class="text-muted-foreground">Clones</dt>
        <dd>{report.clone_count}</dd>
        <dt class="text-muted-foreground">Games</dt>
        <dd data-testid="gauntlet-report-games">
          {report.games_completed} / {report.games_total} completed
        </dd>
        <dt class="text-muted-foreground">Avg days to terminal</dt>
        <dd>{report.average_days_to_terminal.toFixed(2)}</dd>
        <dt class="text-muted-foreground">Avg actions per seat</dt>
        <dd>{report.average_actions_per_seat.toFixed(2)}</dd>
      </dl>
    </Card>

    <Card>
      <h2 class="mb-2 text-sm font-semibold">Faction win rate</h2>
      <ul class="flex flex-col gap-2 text-xs" data-testid="gauntlet-report-faction-bar">
        {#each report.faction_win_rates as entry (entry.faction)}
          <li data-testid="gauntlet-report-faction-row" data-faction={entry.faction}>
            <div class="flex items-baseline justify-between">
              <span class="font-medium">{entry.faction}</span>
              <span class="text-muted-foreground">{entry.wins} / {entry.games}</span>
            </div>
            <div class="relative mt-1 h-2 w-full rounded bg-muted">
              <div
                class="absolute inset-y-0 left-0 rounded bg-primary"
                style={`width: ${Math.min(100, entry.rate.point * 100)}%`}
                data-testid="gauntlet-report-faction-bar-fill"
              ></div>
              <div
                class="absolute inset-y-0 border-l border-r border-dashed border-border"
                style={`left: ${Math.min(100, entry.rate.lower * 100)}%; right: ${Math.max(0, 100 - entry.rate.upper * 100)}%`}
                aria-hidden="true"
              ></div>
            </div>
            <div class="mt-1 text-muted-foreground" data-testid="gauntlet-report-faction-ci">
              {ciLabel(entry.rate.point, entry.rate.lower, entry.rate.upper)}
            </div>
          </li>
        {/each}
      </ul>
    </Card>

    <Card>
      <h2 class="mb-2 text-sm font-semibold">Role family breakdown</h2>
      <table
        class="w-full text-xs"
        data-testid="gauntlet-report-role-family-table"
      >
        <thead class="text-left text-[10px] uppercase tracking-wider text-muted-foreground">
          <tr>
            <th class="pb-1">Family</th>
            <th class="pb-1">G</th>
            <th class="pb-1">W</th>
            <th class="pb-1">D</th>
            <th class="pb-1">L</th>
            <th class="pb-1">Win rate</th>
          </tr>
        </thead>
        <tbody>
          {#each report.role_family_breakdown as row (row.role_family)}
            <tr
              class="border-t border-border"
              data-testid="gauntlet-report-role-family-row"
              data-role-family={row.role_family}
            >
              <td class="py-1 font-mono">{row.role_family}</td>
              <td class="py-1">{row.games}</td>
              <td class="py-1">{row.wins}</td>
              <td class="py-1">{row.draws}</td>
              <td class="py-1">{row.losses}</td>
              <td class="py-1" data-testid="gauntlet-report-role-family-ci">
                {ciLabel(row.win_rate.point, row.win_rate.lower, row.win_rate.upper)}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </Card>

    <Card class="sm:col-span-2 xl:col-span-3">
      <h2 class="mb-2 text-sm font-semibold">Rating deltas</h2>
      {#if report.rating_deltas.length === 0}
        <p class="text-xs text-muted-foreground" data-testid="gauntlet-report-rating-deltas-empty">
          No rating events yet — the gauntlet may still be in flight or the ranked
          rating pipeline has not run for these games.
        </p>
      {:else}
        <ul class="flex flex-col gap-2 text-xs" data-testid="gauntlet-report-rating-deltas">
          {#each report.rating_deltas as delta (delta.agent_build_id + delta.scope_type + delta.scope_value)}
            <li
              class="rounded-md border border-border p-2"
              data-testid="gauntlet-report-rating-delta-row"
              data-agent-build-id={delta.agent_build_id}
            >
              <div class="flex items-baseline justify-between">
                <span class="font-mono">{shortenHash(delta.agent_build_id)}</span>
                <span class="text-muted-foreground">
                  {delta.scope_type}:{delta.scope_value} · {delta.games_in_gauntlet} game{delta.games_in_gauntlet === 1 ? '' : 's'}
                </span>
              </div>
              <div class="mt-1 flex items-center gap-2">
                <span class="text-muted-foreground">μ</span>
                <span>{delta.pre_mu.toFixed(2)} → {delta.post_mu.toFixed(2)}</span>
                <span
                  class={delta.delta_mu >= 0 ? 'text-emerald-500' : 'text-red-500'}
                  data-testid="gauntlet-report-rating-delta-mu"
                >
                  ({sign(delta.delta_mu)})
                </span>
                <span class="ml-3 text-muted-foreground">σ</span>
                <span>{delta.pre_sigma.toFixed(2)} → {delta.post_sigma.toFixed(2)}</span>
                <span class="text-muted-foreground">({sign(delta.delta_sigma)})</span>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </Card>
  </div>
{/if}
