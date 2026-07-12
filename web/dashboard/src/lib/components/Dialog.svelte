<script lang="ts">
  import Button from '$lib/components/Button.svelte';

  interface Props {
    closeLabel?: string;
    description?: string;
    id: string;
    onclose: () => void;
    open: boolean;
    title: string;
    children?: import('svelte').Snippet;
  }

  let {
    closeLabel = 'Close',
    description,
    id,
    onclose,
    open,
    title,
    children
  }: Props = $props();
  let dialog = $state<HTMLDialogElement>();

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  });

  function requestClose(event?: Event): void {
    event?.preventDefault();
    onclose();
  }
</script>

<dialog
  bind:this={dialog}
  aria-describedby={description ? `${id}-description` : undefined}
  aria-labelledby={`${id}-title`}
  class="padrino-dialog"
  oncancel={requestClose}
  onclose={onclose}
>
  <section>
    <div class="flex items-start justify-between gap-4">
      <div>
        <p class="padrino-eyebrow">Padrino laboratory</p>
        <h2 id={`${id}-title`}>{title}</h2>
      </div>
      <Button variant="ghost" class="shrink-0" onclick={() => requestClose()}>{closeLabel}</Button>
    </div>
    {#if description}
      <p class="mt-3 text-sm text-muted-foreground" id={`${id}-description`}>{description}</p>
    {/if}
    {#if children}
      <div class="mt-5">{@render children()}</div>
    {/if}
  </section>
</dialog>
