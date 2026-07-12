<script lang="ts">
  import { cn } from '$lib/utils';

  interface Props {
    class?: string;
    content: string;
    id: string;
    children?: import('svelte').Snippet;
  }

  let { class: className = '', content, id, children }: Props = $props();
</script>

<span class={cn('padrino-tooltip', className)} aria-describedby={id}>
  {@render children?.()}
  <span class="padrino-tooltip__content" id={id} role="tooltip">{content}</span>
</span>
