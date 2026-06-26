<script lang="ts">
  import { goto } from '$app/navigation';
  import { onDestroy, onMount } from 'svelte';
  import { PadrinoApiError } from '$lib/api/client';
  import Button from '$lib/components/Button.svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';

  type MatchStage = 'preparing' | 'seating' | 'deferred' | 'error';

  const MAX_ATTEMPTS = 4;
  const RETRY_DELAYS_MS = [750, 1250, 2000];

  let stage = $state<MatchStage>('preparing');
  let attempt = $state(0);
  let cancelled = false;
  let controller: AbortController | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const stateText = $derived(
    stage === 'deferred'
      ? 'Finding your table'
      : stage === 'seating'
        ? 'Seating your table'
        : stage === 'error'
          ? 'Table not ready'
          : 'Preparing your table'
  );

  const statusText = $derived(
    stage === 'deferred'
      ? `Trying again shortly. Attempt ${Math.min(attempt, MAX_ATTEMPTS)} of ${MAX_ATTEMPTS}.`
      : stage === 'seating'
        ? 'Your seats are ready.'
        : stage === 'error'
          ? 'The table finder stopped after a short wait. You can go back and try again.'
          : 'Setting up a private anonymous table.'
  );

  function isDeferredAdmission(error: unknown): boolean {
    if (!(error instanceof PadrinoApiError)) return false;
    return error.status === 409 || error.status === 423 || error.status === 429 || error.status === 503;
  }

  function isAbort(error: unknown): boolean {
    return error instanceof Error && error.name === 'AbortError';
  }

  function isConsentRequired(error: unknown): boolean {
    return error instanceof PadrinoApiError && error.status === 412;
  }

  function retryDelay(): number {
    return RETRY_DELAYS_MS[Math.min(attempt - 1, RETRY_DELAYS_MS.length - 1)];
  }

  function scheduleRetry(): void {
    retryTimer = setTimeout(() => {
      retryTimer = null;
      void startMatchAttempt();
    }, retryDelay());
  }

  async function startMatchAttempt(): Promise<void> {
    if (cancelled) return;
    attempt += 1;
    stage = attempt === 1 ? 'preparing' : 'deferred';
    controller = new AbortController();

    try {
      const match = await padrino.client.match({ signal: controller.signal });
      if (cancelled) return;
      stage = 'seating';
      await goto(`/play/${encodeURIComponent(match.game_id)}`, { replaceState: true });
    } catch (error) {
      if (cancelled || isAbort(error)) return;
      if (isDeferredAdmission(error) && attempt < MAX_ATTEMPTS) {
        stage = 'deferred';
        scheduleRetry();
        return;
      }
      if (isConsentRequired(error)) {
        // 412: the match endpoint refused for lack of current consent and minted
        // a fresh guest session. Route back to the home 'Play vs AI' CTA, which
        // collects consent inline before re-attempting the match (rather than
        // showing the generic 'table finder stopped' timeout error).
        void goto('/');
        return;
      }
      stage = 'error';
    }
  }

  function cancelMatch(): void {
    cancelled = true;
    controller?.abort();
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    void goto('/', { replaceState: true });
  }

  onMount(() => {
    padrino.setHumanSession(true);
    void startMatchAttempt();
  });

  onDestroy(() => {
    cancelled = true;
    controller?.abort();
    if (retryTimer) clearTimeout(retryTimer);
  });
</script>

<section
  class="mx-auto flex min-h-[55vh] max-w-xl flex-col justify-center"
  data-testid="match-queue-screen"
>
  <Card class="border-primary/30">
    <p class="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
      Play vs AI
    </p>
    <h1 class="text-2xl font-semibold tracking-normal" data-testid="match-state">
      {stateText}
    </h1>
    <p class="mt-3 text-sm text-muted-foreground" data-testid="match-status">
      {statusText}
    </p>

    <div class="mt-5 flex items-center gap-3">
      <Button variant="outline" testid="match-cancel" onclick={cancelMatch}>
        {stage === 'error' ? 'Back to home' : 'Cancel'}
      </Button>
      {#if stage !== 'error'}
        <span class="text-xs text-muted-foreground">This usually takes just a moment.</span>
      {/if}
    </div>
  </Card>
</section>
