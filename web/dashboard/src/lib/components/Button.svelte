<script lang="ts">
  import { cn } from '$lib/utils';

  interface Props {
    class?: string;
    type?: 'button' | 'submit' | 'reset';
    onclick?: (event: MouseEvent) => void;
    disabled?: boolean;
    variant?: 'default' | 'outline' | 'ghost';
    testid?: string;
    children?: import('svelte').Snippet;
  }

  let {
    class: className = '',
    type = 'button',
    onclick,
    disabled = false,
    variant = 'default',
    testid,
    children
  }: Props = $props();

  const base =
    'padrino-button inline-flex items-center justify-center gap-2 px-3 py-2 text-sm disabled:pointer-events-none disabled:opacity-50';
  const variants: Record<string, string> = {
    default: 'padrino-button--primary',
    outline: 'padrino-button--outline',
    ghost: 'padrino-button--ghost'
  };
</script>

<button
  {type}
  {disabled}
  {onclick}
  data-testid={testid}
  class={cn(base, variants[variant], className)}
>
  {@render children?.()}
</button>
