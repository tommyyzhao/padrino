<script lang="ts">
  import { onMount } from 'svelte';
  import Button from '$lib/components/Button.svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { ConsentStatus, LegalDocumentResponse } from '$lib/api/types';

  type RankedPurpose =
    | 'RANKED:PERSISTENT_RATING'
    | 'RANKED:PUBLIC_HANDLE'
    | 'RANKED:CHAT_RETENTION'
    | 'RANKED:ANTICHEAT_PROFILING';

  const PURPOSES: { kind: RankedPurpose; label: string; description: string }[] = [
    {
      kind: 'RANKED:PERSISTENT_RATING',
      label: 'Persistent rating',
      description: 'Write and retain Human ELO rating rows from ranked games.'
    },
    {
      kind: 'RANKED:PUBLIC_HANDLE',
      label: 'Public handle',
      description: 'Show your pseudonymous ranked handle on public leaderboard cards.'
    },
    {
      kind: 'RANKED:CHAT_RETENTION',
      label: 'Chat retention',
      description: 'Retain ranked-game chat sidecar rows for integrity and self-serve flows.'
    },
    {
      kind: 'RANKED:ANTICHEAT_PROFILING',
      label: 'Anti-cheat profiling',
      description: 'Compute advisory integrity signals for manual ranked review.'
    }
  ];

  let document = $state<LegalDocumentResponse | null>(null);
  let consent = $state<ConsentStatus | null>(null);
  let checked = $state<Record<RankedPurpose, boolean>>({
    'RANKED:PERSISTENT_RATING': false,
    'RANKED:PUBLIC_HANDLE': false,
    'RANKED:CHAT_RETENTION': false,
    'RANKED:ANTICHEAT_PROFILING': false
  });
  let submitting = $state(false);
  let error = $state<string | null>(null);
  let statusMessage = $state<string | null>(null);

  const allChecked = $derived(PURPOSES.every((purpose) => checked[purpose.kind]));

  function setChecked(kind: RankedPurpose, event: Event): void {
    checked = { ...checked, [kind]: (event.currentTarget as HTMLInputElement).checked };
  }

  async function load(): Promise<void> {
    padrino.setHumanSession(true);
    error = null;
    try {
      const [documentResult, consentResult] = await Promise.all([
        padrino.client.legalDocument('RANKED'),
        padrino.client.getRankedConsentStatus()
      ]);
      document = documentResult;
      consent = consentResult;
      if (consentResult.consented) {
        for (const purpose of PURPOSES) {
          checked[purpose.kind] = true;
        }
        statusMessage = 'Ranked consent already recorded.';
      }
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function submitRankedConsent(): Promise<void> {
    if (document === null || !allChecked) return;
    submitting = true;
    error = null;
    statusMessage = null;
    try {
      const content_hashes = Object.fromEntries(
        PURPOSES.map((purpose) => [purpose.kind, document?.content_hash ?? ''])
      );
      consent = await padrino.client.postRankedConsent({ content_hashes });
      statusMessage = consent.consented
        ? 'Ranked consent recorded.'
        : 'Ranked consent submitted, but the server still requires re-consent.';
    } catch (e) {
      error = (e as Error).message;
    } finally {
      submitting = false;
    }
  }

  onMount(() => {
    void load();
  });
</script>

<div class="mb-4">
  <a class="text-sm underline" href="/">Home</a>
</div>

<section class="mx-auto max-w-3xl">
  <h1 class="mb-1 text-xl font-semibold">Ranked consent</h1>
  {#if document !== null}
    <p class="mb-4 text-sm text-muted-foreground" data-testid="ranked-consent-version">
      Version {document.version_label}
    </p>
  {/if}

  {#if error}
    <p class="mb-3 text-sm text-red-500" data-testid="ranked-consent-error">{error}</p>
  {/if}

  {#if statusMessage}
    <p class="mb-3 text-sm text-emerald-600" data-testid="ranked-consent-status">
      {statusMessage}
    </p>
  {/if}

  {#if document === null}
    <p class="text-sm text-muted-foreground" data-testid="ranked-consent-loading">
      Loading ranked consent...
    </p>
  {:else}
    <Card>
      <pre
        class="mb-5 whitespace-pre-wrap border-b border-border pb-4 text-sm leading-6"
        data-testid="ranked-consent-document"
      >{document.body}</pre>

      <fieldset class="grid gap-3">
        {#each PURPOSES as purpose (purpose.kind)}
          <label class="rounded border border-border px-3 py-2 text-sm">
            <span class="flex items-start gap-3">
              <input
                class="mt-1"
                type="checkbox"
                data-testid={`ranked-purpose-${purpose.kind}`}
                checked={checked[purpose.kind]}
                onchange={(event) => setChecked(purpose.kind, event)}
              />
              <span>
                <span class="block font-semibold">{purpose.label}</span>
                <span class="block text-muted-foreground">{purpose.description}</span>
              </span>
            </span>
          </label>
        {/each}
      </fieldset>

      <div class="mt-5 flex items-center gap-3">
        <Button
          testid="ranked-consent-submit"
          onclick={() => void submitRankedConsent()}
          disabled={!allChecked || submitting}
        >
          {submitting ? 'Submitting...' : 'Accept ranked consent'}
        </Button>
        <a class="text-sm underline" href="/terms">Terms</a>
        <a class="text-sm underline" href="/privacy">Privacy</a>
      </div>
    </Card>
  {/if}
</section>
