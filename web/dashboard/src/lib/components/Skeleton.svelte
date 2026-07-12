<script lang="ts">
  import { cn } from '$lib/utils';

  interface Props {
    class?: string;
    label?: string;
    testid?: string;
  }

  let { class: className = '', label = 'Loading', testid }: Props = $props();
</script>

<span
  class={cn('padrino-skeleton', className)}
  aria-label={label}
  aria-busy="true"
  data-testid={testid}
></span>
