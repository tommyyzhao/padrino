<script lang="ts">
  import { cn } from '$lib/utils';

  export interface ToastMessage {
    id: string;
    message: string;
    tone?: 'success' | 'warning' | 'error' | 'info';
  }

  interface Props {
    fixed?: boolean;
    items: ToastMessage[];
    testid?: string;
  }

  let { fixed = true, items, testid }: Props = $props();

  function toneClass(tone: ToastMessage['tone']): string {
    return `toast-${tone ?? 'info'}`;
  }
</script>

<div
  class:padrino-toast-region={fixed}
  class:padrino-toast-region--inline={!fixed}
  aria-live="polite"
  aria-relevant="additions"
  data-testid={testid}
>
  {#each items as item (item.id)}
    <p class={cn('padrino-toast', toneClass(item.tone))}>{item.message}</p>
  {/each}
</div>
