import { expect, test, type Page, type Route } from '@playwright/test';

const HOW_TO_PLAY_DISMISSED_KEY = 'padrino:how-to-play-dismissed:v1';
const LEGAL_DOCS = {
  TOS: {
    kind: 'TOS',
    version_label: 'tos-v2',
    content_hash: 'hash-tos-v2',
    body: 'Terms body from the server.\nYou must be 16+ to play.'
  },
  PRIVACY: {
    kind: 'PRIVACY',
    version_label: 'privacy-v2',
    content_hash: 'hash-privacy-v2',
    body: 'Privacy body from the server.\nChat may be stored out of band.'
  },
  AGE_GATE: {
    kind: 'AGE_GATE',
    version_label: 'age-v2',
    content_hash: 'hash-age-v2',
    body: 'Age confirmation body from the server.'
  },
  RANKED: {
    kind: 'RANKED',
    version_label: 'ranked-v3',
    content_hash: 'hash-ranked-v3',
    body:
      'Ranked consent body from the server.\nPersistent rating, public handle, chat retention, and anti-cheat profiling are separate purposes.'
  }
} as const;

function isApi(route: Route): boolean {
  const t = route.request().resourceType();
  return t === 'fetch' || t === 'xhr';
}

async function routeLegalDocuments(page: Page): Promise<void> {
  await page.route('**/legal/documents/*', async (route) => {
    if (!isApi(route)) return route.continue();
    const kind = route.request().url().split('/').pop()?.toUpperCase() ?? '';
    const document = LEGAL_DOCS[kind as keyof typeof LEGAL_DOCS];
    await route.fulfill({
      status: document ? 200 : 404,
      contentType: 'application/json',
      body: JSON.stringify(document ?? { detail: 'legal_document_not_found' })
    });
  });

  await page.route('**/legal/documents', async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ documents: Object.values(LEGAL_DOCS) })
    });
  });
}

async function routeQuietHome(page: Page): Promise<void> {
  await page.route('**/public/live*', async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], total: 0 })
    });
  });

  await page.route('**/public/recent*', async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], next_cursor: null, total_estimate: 0 })
    });
  });

  await page.route('**/public/rulesets', async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items: [
          {
            ruleset_id: 'mini7_v1',
            label: 'Mini 7 canonical team',
            player_count: 7,
            rating_context_kind: 'CANONICAL_TEAM',
            is_canonical: true
          }
        ]
      })
    });
  });

  await page.route('**/public/ladder*', async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ruleset_id: 'mini7_v1', entries: [], next_cursor: null })
    });
  });
}

test.describe('US-309 legal and consent UI', () => {
  test('terms and privacy routes render server-served bodies and version labels', async ({
    page
  }) => {
    await routeLegalDocuments(page);

    await page.goto('/terms');
    await expect(page.getByTestId('legal-doc-title')).toHaveText('Terms & Conditions');
    await expect(page.getByTestId('legal-doc-version')).toContainText('tos-v2');
    await expect(page.getByTestId('legal-doc-body')).toContainText(LEGAL_DOCS.TOS.body);

    await page.goto('/privacy');
    await expect(page.getByTestId('legal-doc-title')).toHaveText('Privacy Policy');
    await expect(page.getByTestId('legal-doc-version')).toContainText('privacy-v2');
    await expect(page.getByTestId('legal-doc-body')).toContainText(LEGAL_DOCS.PRIVACY.body);
  });

  test('home base consent links to legal routes and posts served content hashes', async ({
    page
  }) => {
    await page.addInitScript(
      (key) => window.localStorage.setItem(key, 'true'),
      HOW_TO_PLAY_DISMISSED_KEY
    );
    await routeQuietHome(page);
    await routeLegalDocuments(page);

    let consentPostBody: unknown = null;
    let consented = false;

    await page.route('**/human/me', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ principal_id: 'guest-legal', kind: 'guest', display_name: null })
      });
    });

    await page.route('**/human/consent', async (route) => {
      if (!isApi(route)) return route.continue();
      if (route.request().method() === 'POST') {
        consentPostBody = route.request().postDataJSON();
        consented = true;
        await route.fulfill({
          status: 201,
          contentType: 'application/json',
          body: JSON.stringify({
            consented: true,
            required_versions: { TOS: 'tos-v2', PRIVACY: 'privacy-v2', AGE_GATE: 'age-v2' }
          })
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          consented,
          required_versions: { TOS: 'tos-v2', PRIVACY: 'privacy-v2', AGE_GATE: 'age-v2' }
        })
      });
    });

    await page.route('**/human/wallet', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          granted_micros: 5_000_000,
          available_micros: 5_000_000,
          reserved_micros: 0,
          spent_micros: 0,
          completion_subsidy_micros: 0,
          recent_entries: []
        })
      });
    });
    await page.route('**/human/openrouter/connect', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          connected: false,
          label: null,
          display_suffix: null,
          validated_at: null,
          credit_capability: null
        })
      });
    });

    await page.goto('/');
    await page.getByTestId('home-play-vs-ai-cta').click();
    await expect(page.getByTestId('home-consent-row')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('home-consent-terms-link')).toHaveAttribute('href', '/terms');
    await expect(page.getByTestId('home-consent-privacy-link')).toHaveAttribute('href', '/privacy');

    await page.getByTestId('home-consent-accept').click();
    await expect(page.getByTestId('quick-play-funding')).toBeVisible({ timeout: 15_000 });
    expect(consentPostBody).toEqual({
      content_hashes: {
        TOS: LEGAL_DOCS.TOS.content_hash,
        PRIVACY: LEGAL_DOCS.PRIVACY.content_hash,
        AGE_GATE: LEGAL_DOCS.AGE_GATE.content_hash
      }
    });
  });

  test('a 412 match response re-prompts with the current served document version', async ({
    page
  }) => {
    await page.addInitScript(
      (key) => window.localStorage.setItem(key, 'true'),
      HOW_TO_PLAY_DISMISSED_KEY
    );
    await routeQuietHome(page);
    await routeLegalDocuments(page);

    await page.route('**/human/match', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 412,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'consent_required' })
      });
    });

    await page.goto('/play/match');
    await expect(page).toHaveURL(/\/\?consent=required$/, { timeout: 15_000 });
    await expect(page.getByTestId('home-consent-card')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('home-consent-version')).toContainText('tos-v2');
    await expect(page.getByTestId('home-consent-version')).toContainText('privacy-v2');
  });

  test('ranked consent requires four checkboxes and posts each served hash', async ({
    page
  }) => {
    await routeLegalDocuments(page);

    let rankedPostBody: unknown = null;
    await page.route('**/human/ranked-consent', async (route) => {
      if (!isApi(route)) return route.continue();
      if (route.request().method() === 'POST') {
        rankedPostBody = route.request().postDataJSON();
        await route.fulfill({
          status: 201,
          contentType: 'application/json',
          body: JSON.stringify({
            consented: true,
            required_versions: {
              'RANKED:PERSISTENT_RATING': 'ranked-v3',
              'RANKED:PUBLIC_HANDLE': 'ranked-v3',
              'RANKED:CHAT_RETENTION': 'ranked-v3',
              'RANKED:ANTICHEAT_PROFILING': 'ranked-v3'
            }
          })
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          consented: false,
          required_versions: {
            'RANKED:PERSISTENT_RATING': 'ranked-v3',
            'RANKED:PUBLIC_HANDLE': 'ranked-v3',
            'RANKED:CHAT_RETENTION': 'ranked-v3',
            'RANKED:ANTICHEAT_PROFILING': 'ranked-v3'
          }
        })
      });
    });

    await page.goto('/play/ranked-consent');
    const submit = page.getByTestId('ranked-consent-submit');
    await expect(page.getByTestId('ranked-consent-document')).toContainText(LEGAL_DOCS.RANKED.body);
    await expect(submit).toBeDisabled();

    for (const testId of [
      'ranked-purpose-RANKED:PERSISTENT_RATING',
      'ranked-purpose-RANKED:PUBLIC_HANDLE',
      'ranked-purpose-RANKED:CHAT_RETENTION',
      'ranked-purpose-RANKED:ANTICHEAT_PROFILING'
    ]) {
      await page.getByTestId(testId).check();
    }

    await expect(submit).toBeEnabled();
    await submit.click();
    await expect(page.getByTestId('ranked-consent-status')).toContainText('Ranked consent recorded');
    expect(rankedPostBody).toEqual({
      content_hashes: {
        'RANKED:PERSISTENT_RATING': LEGAL_DOCS.RANKED.content_hash,
        'RANKED:PUBLIC_HANDLE': LEGAL_DOCS.RANKED.content_hash,
        'RANKED:CHAT_RETENTION': LEGAL_DOCS.RANKED.content_hash,
        'RANKED:ANTICHEAT_PROFILING': LEGAL_DOCS.RANKED.content_hash
      }
    });
  });

  test('profile exposes export, delete, and ranked-withdraw endpoint actions', async ({
    page
  }) => {
    let exported = false;
    let deleted = false;
    let withdrew = false;

    await page.route('**/human/me', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          principal_id: 'account-self-serve',
          kind: 'account',
          display_name: 'Ranked Player'
        })
      });
    });
    await page.route('**/human/stats*', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ruleset_id: 'mini7_v1',
          principal_id: 'account-self-serve',
          games: 0,
          wins: 0,
          draws: 0,
          losses: 0,
          role_win_rates: [],
          survival_rate: 0,
          voting_accuracy: { total_votes: 0, accurate_votes: 0, rate: 0 },
          detection_accuracy: '0'
        })
      });
    });
    await page.route('**/human/games*', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [], next_cursor: null, total_estimate: 0 })
      });
    });
    await page.route('**/human/me/export', async (route) => {
      if (!isApi(route)) return route.continue();
      exported = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ schema_version: 'padrino.gdpr_human_export.v1' })
      });
    });
    await page.route('**/human/consent/ranked/withdraw', async (route) => {
      if (!isApi(route)) return route.continue();
      withdrew = true;
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          consented: false,
          required_versions: { 'RANKED:PERSISTENT_RATING': 'ranked-v3' }
        })
      });
    });
    await page.route('**/human/me/delete', async (route) => {
      if (!isApi(route)) return route.continue();
      deleted = true;
      await route.fulfill({ status: 204 });
    });

    await page.goto('/profile');
    await expect(page.getByTestId('profile-self-serve')).toBeVisible();

    await page.getByTestId('profile-export-data').click();
    await expect(page.getByTestId('profile-self-serve-status')).toContainText('Export ready');
    expect(exported).toBe(true);

    await page.getByTestId('profile-withdraw-ranked').click();
    await expect(page.getByTestId('profile-self-serve-status')).toContainText('Ranked consent withdrawn');
    expect(withdrew).toBe(true);

    page.once('dialog', async (dialog) => {
      await dialog.accept();
    });
    await page.getByTestId('profile-delete-account').click();
    await expect(page.getByTestId('profile-self-serve-status')).toContainText('Delete request completed');
    expect(deleted).toBe(true);
  });
});
