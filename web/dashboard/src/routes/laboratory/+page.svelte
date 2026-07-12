<script lang="ts">
  import Badge from '$lib/components/Badge.svelte';
  import Button from '$lib/components/Button.svelte';
  import Card from '$lib/components/Card.svelte';
  import DataList from '$lib/components/DataList.svelte';
  import Dialog from '$lib/components/Dialog.svelte';
  import EndgameRoleBadge from '$lib/components/EndgameRoleBadge.svelte';
  import ModelBadge from '$lib/components/ModelBadge.svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import StatusBadge from '$lib/components/StatusBadge.svelte';
  import Tabs, { type TabItem } from '$lib/components/Tabs.svelte';
  import ToastRegion, { type ToastMessage } from '$lib/components/ToastRegion.svelte';
  import Tooltip from '$lib/components/Tooltip.svelte';

  const tabs: TabItem[] = [
    { id: 'controls', label: 'Controls' },
    { id: 'signals', label: 'Signals' },
    { id: 'data', label: 'Data display' }
  ];
  const toastItems: ToastMessage[] = [
    { id: 'saved', message: 'Lobby settings saved locally.', tone: 'success' }
  ];

  let activeTab = $state('controls');
  let dialogOpen = $state(false);
</script>

<section class="mx-auto max-w-5xl" data-testid="design-system-shell">
  <div class="mb-8 max-w-3xl">
    <p class="padrino-eyebrow">Interface field guide</p>
    <h1 class="mt-2 text-3xl font-bold tracking-tight sm:text-4xl" data-testid="design-system-title">
      Neo-noir occult laboratory
    </h1>
    <p class="mt-3 text-base leading-7 text-muted-foreground">
      A dark-first interface foundation for legible games, precise instrumentation, and dramatic
      moments that never use visual treatment to disclose hidden table information.
    </p>
  </div>

  <Tabs
    ariaLabel="Laboratory component groups"
    items={tabs}
    selected={activeTab}
    onselect={(id) => (activeTab = id)}
    testid="design-system-tabs"
  />

  <div class="mt-5 grid gap-5 lg:grid-cols-[1.2fr_0.8fr]">
    <Card class="padrino-card--interactive" data-testid="design-system-controls">
      {#if activeTab === 'controls'}
        <p class="padrino-eyebrow">Accessible controls</p>
        <h2 class="mt-2 text-xl font-semibold">Clear actions, visible focus</h2>
        <p class="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">
          All controls have a keyboard focus treatment, generous target size, and a reduced-motion
          fallback. Their visual state carries only the action state, never player identity.
        </p>
        <div class="mt-5 flex flex-wrap gap-3">
          <Button testid="design-system-primary">Primary action</Button>
          <Button variant="outline" testid="design-system-dialog-open" onclick={() => (dialogOpen = true)}>
            Open dialog
          </Button>
          <Tooltip id="design-system-tooltip" content="A concise explanation remains available on focus.">
            <button
              class="padrino-button padrino-button--ghost px-3 py-2 text-sm"
              type="button"
              data-testid="design-system-tooltip-trigger"
            >
              Focus tip
            </button>
          </Tooltip>
        </div>
      {:else if activeTab === 'signals'}
        <p class="padrino-eyebrow">Semantic signals</p>
        <h2 class="mt-2 text-xl font-semibold">Status is semantic, not decorative</h2>
        <div class="mt-5 flex flex-wrap gap-2" data-testid="design-system-statuses">
          <StatusBadge status="live" label="Live" />
          <StatusBadge status="waiting" label="Waiting" />
          <StatusBadge status="warning" label="Review" />
          <StatusBadge status="error" label="Action needed" />
          <ModelBadge tier="ranked-qualified" label="Ranked qualified" />
          <ModelBadge tier="provisional" label="Provisional" />
        </div>
        <p class="mt-5 text-sm leading-6 text-muted-foreground">
          Role and faction tokens are terminal-reveal-only. Their component requires an explicit
          reveal boundary; live seats remain neutral.
        </p>
        <div class="mt-3 flex flex-wrap gap-2" data-testid="design-system-reveal-example">
          <EndgameRoleBadge role="DETECTIVE" faction="TOWN" revealed={true} />
          <EndgameRoleBadge role="MAFIA" faction="MAFIA" revealed={true} />
        </div>
      {:else}
        <p class="padrino-eyebrow">Data display</p>
        <h2 class="mt-2 text-xl font-semibold">Quiet technical instrumentation</h2>
        <DataList
          testid="design-system-data-list"
          items={[
            { label: 'Phase window', value: '02:14 remaining' },
            { label: 'Events committed', value: '42' },
            { label: 'Replay state', value: 'Hash verified' }
          ]}
        />
      {/if}
    </Card>

    <div class="grid gap-5">
      <Card>
        <p class="padrino-eyebrow">Loading state</p>
        <h2 class="mt-2 text-lg font-semibold">No layout jump</h2>
        <div class="mt-4 grid gap-2" aria-label="Loading example">
          <Skeleton class="w-full" testid="design-system-skeleton" />
          <Skeleton class="w-4/5" />
          <Skeleton class="w-2/5" />
        </div>
      </Card>
      <Card>
        <p class="padrino-eyebrow">Safe seat state</p>
        <h2 class="mt-2 text-lg font-semibold">Identity-blind in play</h2>
        <div class="mt-4 flex flex-wrap gap-2">
          <Badge tone="seat-alive">Active seat</Badge>
          <Badge tone="seat-eliminated">Eliminated seat</Badge>
        </div>
        <p class="mt-3 text-sm leading-6 text-muted-foreground">
          Presence and alive state are the only seat-level visual signals before endgame reveal.
        </p>
        <ToastRegion items={toastItems} fixed={false} testid="design-system-toasts" />
      </Card>
    </div>
  </div>
</section>

<Dialog
  id="design-system-dialog"
  title="Confirm laboratory action"
  description="This preview demonstrates the native, keyboard-accessible dialog treatment."
  open={dialogOpen}
  onclose={() => (dialogOpen = false)}
>
  <div class="flex justify-end gap-3">
    <Button variant="outline" onclick={() => (dialogOpen = false)}>Cancel</Button>
    <Button onclick={() => (dialogOpen = false)}>Confirm</Button>
  </div>
</Dialog>
