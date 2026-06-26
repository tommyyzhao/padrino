<script lang="ts">
  import { goto } from '$app/navigation';
  import Button from '$lib/components/Button.svelte';
  import HowToPlayPanel from '$lib/components/HowToPlayPanel.svelte';
  import { dismissHowToPlay } from '$lib/howToPlay';

  function dismissAndReturn(): void {
    dismissHowToPlay();
    void goto('/#play-vs-ai');
  }
</script>

<svelte:head>
  <title>How to play | Padrino</title>
</svelte:head>

<div class="mx-auto max-w-3xl">
  <HowToPlayPanel>
    <Button testid="how-to-play-dismiss" onclick={dismissAndReturn}>Got it</Button>
    <a class="text-sm underline-offset-2 hover:underline" href="/#play-vs-ai">Back to play</a>
  </HowToPlayPanel>
</div>
