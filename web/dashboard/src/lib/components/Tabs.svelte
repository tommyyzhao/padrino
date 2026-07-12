<script lang="ts">
  import { cn } from '$lib/utils';

  export interface TabItem {
    id: string;
    label: string;
  }

  interface Props {
    ariaLabel: string;
    class?: string;
    items: TabItem[];
    onselect?: (id: string) => void;
    selected?: string;
    testid?: string;
  }

  let {
    ariaLabel,
    class: className = '',
    items,
    onselect,
    selected: selectedProp,
    testid
  }: Props = $props();
  let selected = $state<string>('');

  $effect(() => {
    if (selectedProp !== undefined) {
      selected = selectedProp;
    } else if (selected === '' && items[0] !== undefined) {
      selected = items[0].id;
    }
  });

  function select(id: string): void {
    selected = id;
    onselect?.(id);
  }
</script>

<div class={cn('padrino-tabs', className)} role="tablist" aria-label={ariaLabel} data-testid={testid}>
  {#each items as item (item.id)}
    <button
      type="button"
      role="tab"
      aria-selected={selected === item.id}
      class:active={selected === item.id}
      onclick={() => select(item.id)}
    >
      {item.label}
    </button>
  {/each}
</div>
