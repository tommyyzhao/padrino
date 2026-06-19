import {
  PadrinoClient,
  loadApiKeyFromSession,
  resolveBaseUrl,
  saveApiKeyToSession
} from './api/client';

function createClientStore() {
  let apiKey = $state<string | null>(null);
  // Human-session mode (Wave 9): the play client authenticates via the backend's
  // http-only session cookie rather than the spectator API key. The cookie is
  // never readable here, so the store only tracks whether to send credentials.
  let humanSession = $state(false);
  const baseUrl = resolveBaseUrl();
  const client = new PadrinoClient({ baseUrl, apiKey: null });

  function init() {
    if (typeof window === 'undefined') return;
    const stored = loadApiKeyFromSession();
    if (stored) {
      apiKey = stored;
      client.setApiKey(stored);
    }
  }

  function setKey(key: string | null) {
    apiKey = key;
    client.setApiKey(key);
    saveApiKeyToSession(key);
  }

  function setHumanSession(enabled: boolean) {
    humanSession = enabled;
    client.setHumanSession(enabled);
  }

  return {
    get client() {
      return client;
    },
    get apiKey() {
      return apiKey;
    },
    get humanSession() {
      return humanSession;
    },
    get baseUrl() {
      return baseUrl;
    },
    init,
    setKey,
    setHumanSession
  };
}

export const padrino = createClientStore();
