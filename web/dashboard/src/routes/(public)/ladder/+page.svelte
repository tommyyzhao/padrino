<script lang="ts">
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import { canonicalTeamRulesets } from '$lib/rulesets';
  import type { PublicLadderEntry, PublicRulesetEntry } from '$lib/api/types';

  let rulesets = $state<PublicRulesetEntry[]>([]);
  let ruleset = $state<string | null>(null);
  let entries = $state<PublicLadderEntry[]>([]);
  let rulesetsLoading = $state(true);
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function load(selectedRuleset: string) {
    loading = true;
    error = null;
    try {
      const response = await padrino.client.publicLadder({ ruleset_id: selectedRuleset, limit: 50 });
      entries = response.entries;
    } catch (e) {
      error = (e as Error).message;
      entries = [];
    } finally {
      loading = false;
    }
  }

  async function loadRulesets() {
    rulesetsLoading = true;
    error = null;
    try {
      const response = await padrino.client.publicRulesets();
      rulesets = canonicalTeamRulesets(response.items);
      if (rulesets.length === 0) {
        ruleset = null;
        entries = [];
        error = 'No canonical ladder rulesets available.';
        return;
      }
      const selected =
        ruleset !== null && rulesets.some((option) => option.ruleset_id === ruleset)
          ? ruleset
          : rulesets[0].ruleset_id;
      ruleset = selected;
      await load(selected);
    } catch (e) {
      rulesets = [];
      ruleset = null;
      entries = [];
      loading = false;
      error = (e as Error).message;
    } finally {
      rulesetsLoading = false;
    }
  }

  function switchRuleset(r: string) {
    ruleset = r;
    void load(r);
  }

  onMount(() => {
    void loadRulesets();
  });
</script>

<div class="mb-4">
  <a class="text-sm underline" href="/">← Home</a>
</div>

<h1 class="mb-4 text-xl font-semibold">Ladder</h1>

<div class="mb-4 flex gap-2" data-testid="ladder-ruleset-switch">
  {#if rulesetsLoading}
    <span class="text-sm text-muted-foreground" data-testid="ladder-rulesets-loading">
      Loading rulesets…
    </span>
  {:else}
    {#each rulesets as option (option.ruleset_id)}
      <button
        class={'rounded px-3 py-1 text-sm ' +
          (option.ruleset_id === ruleset
            ? 'bg-primary text-primary-foreground'
            : 'bg-muted text-muted-foreground hover:bg-muted/80')}
        data-testid="ladder-ruleset-btn"
        data-ruleset={option.ruleset_id}
        data-active={option.ruleset_id === ruleset ? 'true' : 'false'}
        onclick={() => switchRuleset(option.ruleset_id)}
      >
        {option.label} ({option.ruleset_id})
      </button>
    {/each}
  {/if}
</div>

{#if loading}
  <p class="text-sm text-muted-foreground" data-testid="ladder-loading">Loading…</p>
{:else if error}
  <p class="text-sm text-red-500" data-testid="ladder-error">{error}</p>
{:else if entries.length === 0}
  <p class="text-sm text-muted-foreground" data-testid="ladder-empty">No ranked agents yet.</p>
{:else}
  <Card>
    <table class="w-full text-sm" data-testid="ladder-table">
      <thead class="text-left text-xs uppercase tracking-wider text-muted-foreground">
        <tr>
          <th class="pb-2 pr-4">Rank</th>
          <th class="pb-2 pr-4">Agent</th>
          <th class="pb-2 pr-4">Version</th>
          <th class="pb-2 pr-4 text-right">Ordinal</th>
          <th class="pb-2 pr-4 text-right">Games</th>
          <th class="pb-2 text-right">Last Active</th>
        </tr>
      </thead>
      <tbody>
        {#each entries as entry, i (entry.agent_build_id)}
          <tr class="border-t border-border" data-testid="ladder-row" data-build-id={entry.agent_build_id}>
            <td class="py-2 pr-4">{i + 1}</td>
            <td class="py-2 pr-4">
              <a
                href="/models/{entry.agent_build_id}"
                class="font-medium underline-offset-2 hover:underline"
                data-testid="ladder-agent-link"
              >
                {entry.display_name}
              </a>
              {#if entry.provisional}
                <span
                  class="ml-1 rounded bg-amber-100 px-1 py-0.5 text-xs text-amber-700"
                  data-testid="ladder-provisional-badge"
                >
                  provisional
                </span>
              {:else}
                <span
                  class="ml-1 rounded bg-emerald-100 px-1 py-0.5 text-xs text-emerald-700"
                  data-testid="ladder-established-badge"
                >
                  established
                </span>
              {/if}
            </td>
            <td class="py-2 pr-4 font-mono text-xs text-muted-foreground">{entry.version}</td>
            <td class="py-2 pr-4 text-right font-semibold" data-testid="ladder-ordinal">
              {entry.ordinal}
            </td>
            <td class="py-2 pr-4 text-right">{entry.games}</td>
            <td class="py-2 text-right text-xs text-muted-foreground">
              {entry.last_game_at ? new Date(entry.last_game_at).toLocaleDateString() : '—'}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </Card>
{/if}
