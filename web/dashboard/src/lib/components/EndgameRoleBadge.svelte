<script lang="ts">
  import Badge from '$lib/components/Badge.svelte';
  import { factionBadgeTone, roleBadgeTone } from '$lib/design/tokens';

  interface Props {
    faction: string;
    revealed: boolean;
    role: string;
    testid?: string;
  }

  let { faction, revealed, role, testid }: Props = $props();
</script>

{#if revealed}
  <span class="inline-flex flex-wrap items-center gap-1" data-testid={testid}>
    <Badge tone={roleBadgeTone(role)}>{role}</Badge>
    <Badge tone={factionBadgeTone(faction)}>{faction}</Badge>
  </span>
{:else}
  <Badge tone="status-neutral" {testid}>Information hidden</Badge>
{/if}
