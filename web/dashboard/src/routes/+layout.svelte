<script lang="ts">
  import '../app.css';
  import { padrino } from '$lib/clientStore.svelte';
  import { onMount } from 'svelte';
  import { page } from '$app/stores';

  interface Props {
    children?: import('svelte').Snippet;
  }

  let { children }: Props = $props();

  onMount(() => {
    padrino.init();
  });

  // US-411: recording layout hides site chrome on spectator pages.
  const recording = $derived(
    $page.url.pathname.startsWith('/watch/') && $page.url.searchParams.get('record') === '1'
  );
</script>

<div class="padrino-shell min-h-screen">
  {#if !recording}
  <header class="border-b border-border bg-card/90 backdrop-blur">
    <div class="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3 sm:py-4">
      <a href="/" class="group flex min-w-0 items-center gap-2.5" aria-label="Padrino home">
        <!-- Original geometric mark, created for Padrino; no third-party asset license applies. -->
        <img class="h-8 w-8 shrink-0" src="/brand/observatory-mark.svg" alt="" />
        <span class="min-w-0">
          <span class="padrino-wordmark block text-lg font-bold leading-none">Padrino</span>
          <span class="mt-1 block text-[0.6rem] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
            Occult laboratory
          </span>
        </span>
      </a>
      <nav class="flex flex-wrap items-center justify-end gap-x-3 gap-y-2 text-sm sm:gap-x-4" aria-label="Primary navigation">
        <a class="font-medium text-muted-foreground hover:text-foreground hover:underline" href="/">Home</a>
        <a
          class="padrino-button padrino-button--primary inline-flex min-h-9 items-center justify-center px-3 py-1.5 text-sm"
          href="/#game-mode-home"
          data-testid="nav-play-vs-ai"
        >
          Quick Play
        </a>
        <a class="font-medium text-muted-foreground hover:text-foreground hover:underline" href="/ladder">Ladder</a>
        <a class="font-medium text-muted-foreground hover:text-foreground hover:underline" href="/models">Models</a>
        <a class="font-medium text-muted-foreground hover:text-foreground hover:underline" href="/leaderboard" data-testid="nav-leaderboard">Leaderboard</a>
        <a class="font-medium text-muted-foreground hover:text-foreground hover:underline" href="/lobby" data-testid="nav-lobby">Custom Lobby</a>
      </nav>
    </div>
  </header>
  {/if}
  <main class="mx-auto max-w-6xl px-4 py-6 sm:py-8">
    {@render children?.()}
  </main>
  {#if !recording}
  <footer class="mt-8 border-t border-border py-5 text-center text-xs text-muted-foreground">
    Padrino · deterministic social deduction · API <code>{padrino.baseUrl}</code>
  </footer>
  {/if}
</div>
