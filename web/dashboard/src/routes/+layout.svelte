<script lang="ts">
  import '../app.css';
  import { padrino } from '$lib/clientStore.svelte';
  import { onMount } from 'svelte';

  interface Props {
    children?: import('svelte').Snippet;
  }

  let { children }: Props = $props();

  onMount(() => {
    padrino.init();
  });
</script>

<div class="min-h-screen">
  <header class="border-b border-border bg-card">
    <div class="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
      <a href="/" class="text-lg font-semibold">Padrino</a>
      <nav class="flex gap-4 text-sm">
        <a class="hover:underline" href="/">Home</a>
        <a class="hover:underline" href="/leaderboard">Leaderboard</a>
        <a class="hover:underline" href="/games">Games</a>
      </nav>
    </div>
  </header>
  <main class="mx-auto max-w-6xl px-4 py-6">
    {@render children?.()}
  </main>
  <footer class="border-t border-border py-4 text-center text-xs text-muted-foreground">
    Padrino dashboard · API base: <code>{padrino.baseUrl}</code>
  </footer>
</div>
