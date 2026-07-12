<script lang="ts">
  export interface DataItem {
    label: string;
    value: string;
  }

  interface Props {
    items: DataItem[];
    testid?: string;
  }

  let { items, testid }: Props = $props();
</script>

<dl class="padrino-data-list" data-testid={testid}>
  {#each items as item (item.label)}
    <div>
      <dt>{item.label}</dt>
      <dd>{item.value}</dd>
    </div>
  {/each}
</dl>
