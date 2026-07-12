<script lang="ts">
  import Badge from '$lib/components/Badge.svelte';
  import { modelTierBadgeTone } from '$lib/design/tokens';

  interface Props {
    tier: string;
    label?: string;
    testid?: string;
  }

  let { tier, label = tier, testid }: Props = $props();
</script>

<Badge tone={modelTierBadgeTone(tier)} {testid}>{label}</Badge>
