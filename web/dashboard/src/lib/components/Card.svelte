<script lang="ts">
  import { cn } from '$lib/utils';

  interface Props {
    class?: string;
    children?: import('svelte').Snippet;
    [key: string]: unknown;
  }

  let { class: className = '', children, ...rest }: Props = $props();
</script>

<div class={cn('padrino-card p-4 sm:p-5', className)} {...rest}>
  {@render children?.()}
</div>
