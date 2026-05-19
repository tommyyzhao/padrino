<script lang="ts">
  import Button from './Button.svelte';

  interface Props {
    submit: (key: string) => void;
  }

  let { submit }: Props = $props();
  let value = $state('');

  function onSubmit() {
    if (value.trim() === '') return;
    submit(value.trim());
  }
</script>

<form
  class="mx-auto flex max-w-md flex-col gap-3 rounded-lg border border-border bg-card p-6 shadow-sm"
  onsubmit={(e: SubmitEvent) => {
    e.preventDefault();
    onSubmit();
  }}
>
  <h2 class="text-lg font-semibold">Spectator key required</h2>
  <p class="text-sm text-muted-foreground">
    This deployment requires a Padrino API key with the spectator scope. The key is kept in
    <code>sessionStorage</code> only — never in localStorage or cookies.
  </p>
  <input
    type="password"
    autocomplete="off"
    placeholder="pk_..."
    bind:value
    class="rounded-md border border-border bg-background px-3 py-2 text-sm"
  />
  <Button type="submit">Save key for this session</Button>
</form>
