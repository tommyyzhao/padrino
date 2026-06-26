export const HOW_TO_PLAY_DISMISSED_KEY = 'padrino:how-to-play-dismissed:v1';

function localStore(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function hasDismissedHowToPlay(): boolean {
  return localStore()?.getItem(HOW_TO_PLAY_DISMISSED_KEY) === 'true';
}

export function dismissHowToPlay(): void {
  localStore()?.setItem(HOW_TO_PLAY_DISMISSED_KEY, 'true');
}
