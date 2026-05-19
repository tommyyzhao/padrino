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
    'inline-flex items-center justify-center rounded-md px-3 py-1.5 text-sm font-medium transition disabled:opacity-50 disabled:pointer-events-none';
  const variants: Record<string, string> = {
    default: 'bg-primary text-background hover:opacity-90',
    outline: 'border border-border hover:bg-accent',
    ghost: 'hover:bg-accent'
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
