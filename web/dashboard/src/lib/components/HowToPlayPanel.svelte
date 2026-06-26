<script lang="ts">
  import { cn } from '$lib/utils';

  interface Props {
    class?: string;
    children?: import('svelte').Snippet;
  }

  let { class: className = '', children }: Props = $props();
</script>

<section
  class={cn('rounded-lg border border-border bg-card p-5 shadow-sm sm:p-6', className)}
  data-testid="how-to-play-panel"
>
  <p class="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
    Quick rules
  </p>
  <h1 id="how-to-play-title" class="text-2xl font-semibold tracking-normal">
    How to play Mafia
  </h1>
  <p class="mt-3 max-w-2xl text-sm text-muted-foreground">
    Two teams share one table. Town tries to find the Mafia before the Mafia survives long
    enough to control the vote.
  </p>

  <div class="mt-5 grid gap-3 sm:grid-cols-2">
    <section class="rounded-md border border-border p-3" data-testid="how-to-play-roles">
      <h2 class="text-sm font-semibold">Town vs Mafia</h2>
      <p class="mt-2 text-sm text-muted-foreground">
        Villagers discuss and vote. The Detective can investigate at night. The Doctor can
        protect at night. Mafia secretly choose a night target.
      </p>
    </section>

    <section class="rounded-md border border-border p-3" data-testid="how-to-play-day">
      <h2 class="text-sm font-semibold">Day discussion + vote</h2>
      <p class="mt-2 text-sm text-muted-foreground">
        During the day discussion, everyone talks publicly. Then living players vote. A unique
        top vote can eliminate one player; ties or abstains can spare the table.
      </p>
    </section>

    <section class="rounded-md border border-border p-3" data-testid="how-to-play-night">
      <h2 class="text-sm font-semibold">Night actions</h2>
      <p class="mt-2 text-sm text-muted-foreground">
        At night, Mafia pick a kill target, the Doctor protects, and the Detective investigates.
        Actions resolve together before the next day.
      </p>
    </section>

    <section class="rounded-md border border-border p-3" data-testid="how-to-play-win">
      <h2 class="text-sm font-semibold">How teams win</h2>
      <p class="mt-2 text-sm text-muted-foreground">
        Town wins when no Mafia remain. Mafia wins when they equal or outnumber Town.
      </p>
    </section>
  </div>

  <section
    class="mt-3 rounded-md border border-border bg-accent p-3"
    data-testid="how-to-play-spot-ai"
  >
    <h2 class="text-sm font-semibold">Spot the AI</h2>
    <p class="mt-2 text-sm text-muted-foreground">
      Some players are AI. Identities stay hidden during play. At the reveal, guess which
      players were AI; your detection accuracy is scored.
    </p>
  </section>

  {#if children}
    <div class="mt-5 flex flex-wrap items-center gap-3">
      {@render children()}
    </div>
  {/if}
</section>
