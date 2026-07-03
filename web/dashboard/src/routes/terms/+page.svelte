<script lang="ts">
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { LegalDocumentResponse } from '$lib/api/types';

  let document = $state<LegalDocumentResponse | null>(null);
  let error = $state<string | null>(null);

  onMount(async () => {
    try {
      document = await padrino.client.legalDocument('TOS');
    } catch (e) {
      error = (e as Error).message;
    }
  });
</script>

<div class="mb-4">
  <a class="text-sm underline" href="/">Home</a>
</div>

<h1 class="mb-1 text-xl font-semibold" data-testid="legal-doc-title">Terms & Conditions</h1>

{#if error}
  <p class="text-sm text-red-500" data-testid="legal-doc-error">{error}</p>
{:else if document === null}
  <p class="text-sm text-muted-foreground" data-testid="legal-doc-loading">Loading terms...</p>
{:else}
  <p class="mb-4 text-sm text-muted-foreground" data-testid="legal-doc-version">
    Version {document.version_label}
  </p>
  <Card>
    <pre class="whitespace-pre-wrap text-sm leading-6" data-testid="legal-doc-body">{document.body}</pre>
  </Card>
{/if}
