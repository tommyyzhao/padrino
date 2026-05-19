import {
  PadrinoClient,
  loadApiKeyFromSession,
  resolveBaseUrl,
  saveApiKeyToSession
} from './api/client';

function createClientStore() {
  let apiKey = $state<string | null>(null);
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

  return {
    get client() {
      return client;
    },
    get apiKey() {
      return apiKey;
    },
    get baseUrl() {
      return baseUrl;
    },
    init,
    setKey
  };
}

export const padrino = createClientStore();
