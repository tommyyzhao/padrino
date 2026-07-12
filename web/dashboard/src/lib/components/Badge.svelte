<script lang="ts">
  import { cn } from '$lib/utils';

  interface Props {
    class?: string;
    tone?: string;
    testid?: string;
    children?: import('svelte').Snippet;
  }

  let { class: className = '', tone = 'status-neutral', testid, children }: Props = $props();
</script>

<span class={cn('padrino-badge', tone, className)} data-testid={testid}>
  {@render children?.()}
</span>
