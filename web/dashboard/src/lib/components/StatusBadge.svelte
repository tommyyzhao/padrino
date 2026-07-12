<script lang="ts">
  import Badge from '$lib/components/Badge.svelte';
  import { statusBadgeTone } from '$lib/design/tokens';

  interface Props {
    status: string;
    label?: string;
    testid?: string;
  }

  let { status, label = status, testid }: Props = $props();
</script>

<Badge tone={statusBadgeTone(status)} {testid}>{label}</Badge>
