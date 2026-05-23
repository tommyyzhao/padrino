# Bring-your-own-model

Padrino routes every LLM call through [LiteLLM][litellm], so any provider
LiteLLM supports — OpenAI, Anthropic, Cerebras, DeepInfra, Mistral, Groq,
Ollama, and more — can host a Padrino agent. This guide explains how to
add a new provider to a running deployment, what the data model looks
like, and how to keep the contract suite green so future releases don't
silently break against your model.

[litellm]: https://docs.litellm.ai/docs/providers

## The data model — `ModelProvider`, `ModelConfig`, `AgentBuild`

Padrino separates "who serves the bytes" from "what an agent is". The
three tables map cleanly onto three different operational questions:

| Table             | Question it answers                                          | Mutable?                    |
|-------------------|--------------------------------------------------------------|-----------------------------|
| `model_providers` | Where do we send the HTTP call? What credential do we use?   | Yes (rotate keys).          |
| `model_configs`   | Which model on that provider, with what default sampling?    | Yes (new versions over time). |
| `agent_builds`    | A pinned (model_config, prompt_version, inference_params) triple — the immutable identity that gets a rating. | **No** — every change is a new row.  |

A few invariants that follow:

- Two agents with different prompt versions are two different
  `AgentBuild` rows, even if they share the same `ModelConfig`. The
  leaderboard tracks them independently.
- Rotating a provider's API key updates `ModelProvider.auth_secret_ref`
  (or the file it points at). No `AgentBuild` row changes — the rating
  history stays intact.
- Swapping a provider's underlying model version (e.g. Cerebras releases
  `zai-glm-4.8`) is a NEW `ModelConfig` row pointing at a new LiteLLM
  model id. Any `AgentBuild` that wants to use the new model is also a
  new row with a fresh rating. **Never** mutate a `ModelConfig` to point
  at a different upstream model — the rating chain would be silently
  contaminated.

The `auth_secret_ref` on `ModelProvider` is one of:

- `env:VAR_NAME` — read `$VAR_NAME` from the process environment.
- `file:/absolute/path` — read the file (must be `chmod 600`).

See US-050's `padrino.llm.secrets.resolve_secret` for the full grammar.

## Adding a provider — bootstrap path

Drop a `providers.yaml` next to your `.env` and pass it on bootstrap:

```yaml
providers:
  - name: openai
    auth_secret_ref: env:OPENAI_API_KEY
    base_url: https://api.openai.com
    default_model: gpt-4o-mini
    timeout_s: 45.0
  - name: ollama-local
    auth_secret_ref: env:OLLAMA_API_KEY
    base_url: http://localhost:11434
    default_model: llama3
    timeout_s: 120.0
```

```
uv run padrino bootstrap --providers ./providers.yaml
```

The bootstrap step is idempotent: re-runs skip providers that already
exist by name. To rotate a credential **change** an existing provider's
`auth_secret_ref`, run `padrino bootstrap` again, and then HUP the api
+ scheduler containers so the adapters pick up the new value. The
adapter resolves the secret **once** at construction time and caches it
on `self._auth_secret`, so a hot-reload requires a process restart.

## Adding a provider — live API path

Against a running deployment, mint an admin api key and call the
`/admin/model-providers` endpoint:

```
curl -X POST https://padrino.example.org/admin/model-providers \
     -H "Authorization: Bearer <admin-raw-key>" \
     -H "Content-Type: application/json" \
     -d '{
           "name": "anthropic",
           "auth_secret_ref": "env:ANTHROPIC_API_KEY",
           "base_url": "https://api.anthropic.com"
         }'
```

The route eagerly resolves `auth_secret_ref` (US-050) so a missing env
var or unreadable secret file fails with a 422 immediately instead of a
401 at game time. Follow up with `POST /admin/model-configs` to declare
the specific model on that provider, and `POST /admin/agent-builds` to
mint the rated identity. Each call returns the newly-created row's id
so you can chain.

## LiteLLM model identifiers

For native LiteLLM providers, the full model id the adapter sends is
usually `<provider>/<model>`. For OpenAI-compatible providers hosted at a
custom endpoint, set `ModelProvider.base_url` and store the exact
LiteLLM dispatch id on `ModelConfig.litellm_model_id`:

| Provider     | Example identifier                                |
|--------------|---------------------------------------------------|
| OpenAI       | `openai/gpt-4o-mini`                              |
| Anthropic    | `anthropic/claude-haiku-4-5`                      |
| Cerebras     | `cerebras/zai-glm-4.7`                            |
| DeepInfra    | `deepinfra/deepseek-ai/DeepSeek-V4-Flash`         |
| Xiaomi       | `openai/mimo-v2.5` with `base_url=https://token-plan-sgp.xiaomimimo.com/v1` |
| Groq         | `groq/llama-3.1-70b-versatile`                    |
| Mistral      | `mistral/mistral-large-latest`                    |
| Ollama       | `ollama/llama3` (note: dispatches to `/api/generate`, not `/api/chat`) |

If LiteLLM picks the wrong endpoint shape for your provider (some
upstreams disagree about whether to use OpenAI-compat `/chat/completions`
vs. their native chat format), set `base_url` on the `ModelProvider`
row to pin the override.

## Same-model multi-host fallback (US-079)

A single model identity can be served by multiple provider endpoints.
GLM-4.7 is hosted by Cerebras *and* by Z.AI (and a handful of others)
loading the same upstream weights. When the primary host hits a 429,
times out, or returns a 5xx, the adapter can route the call to an
alternate host **serving the same model** before falling through to
the regular different-model fallback. The leaderboard keeps a single
row per `(provider, model_name, model_version)` of the *AgentBuild* —
which host actually served any given call is observable only in
`llm_calls.status` (`same_model_fallback_ok` vs. `ok`).

`RoutingPolicy` carries an optional `same_model_hosts: tuple[SameModelHost, ...]`
that the adapter iterates between the primary attempts and the
different-model `fallback_model`. Each host has its own retry budget
(same-host retries never consume cross-host attempts) and its own
credential resolved at adapter construction time, so a misconfigured
`auth_secret_ref` fails loudly at boot rather than silently on a 401.

The canonical built-in pairing routes Cerebras `zai-glm-4.7` to Z.AI's
`openai/glm-4.7` endpoint. It is enabled by default and engaged
automatically by `Settings.build_routing_policy()` whenever:

1. `padrino_cerebras_zai_glm47_zai_fallback` is `True` (default), and
2. `ZAI_API_KEY` resolves in the environment, and
3. The primary model is `cerebras/zai-glm-4.7`.

The Z.AI base URL defaults to the **Coding Plan** endpoint
(`https://api.z.ai/api/coding/paas/v4`) since that's our paid
subscription tier. If you are on the General API plan instead, override
via `PADRINO_ZAI_API_BASE=https://api.z.ai/api/paas/v4`.

To disable the fallback (e.g. while debugging a Z.AI outage):

```
PADRINO_CEREBRAS_ZAI_GLM47_ZAI_FALLBACK=false uv run padrino ...
```

To wire a same-model fallback for another model identity, build the
`RoutingPolicy` explicitly:

```python
from padrino.llm.adapter import RoutingPolicy, SameModelHost

policy = RoutingPolicy(
    primary_model="cerebras/zai-glm-4.7",
    fallback_model="deepinfra/deepseek-ai/DeepSeek-V4-Flash",
    same_model_hosts=(
        SameModelHost(
            provider="zai",
            litellm_model_id="openai/glm-4.7",
            api_base="https://api.z.ai/api/coding/paas/v4",
            auth_secret_ref="env:ZAI_API_KEY",
        ),
    ),
)
```

Order matters: hosts are tried left-to-right. The integration test
`tests/integration/test_same_model_fallback.py` exercises the full
path against the real Cerebras + Z.AI endpoints — it monkeypatches the
primary call to raise `litellm.exceptions.RateLimitError` and asserts
the result lands with `status == 'same_model_fallback_ok'`.

## Xiaomi token-plan (US-082)

Xiaomi's token-plan endpoint is OpenAI-compatible, but it is not served
from OpenAI. Register the provider with `base_url`; the adapter forwards
that value to LiteLLM as `api_base` and passes the credential resolved
from `auth_secret_ref` as `api_key`.

```yaml
providers:
  - name: xiaomi
    auth_secret_ref: env:XIAOMI_API_KEY
    base_url: https://token-plan-sgp.xiaomimimo.com/v1
    models:
      - model_name: mimo-v2.5
        litellm_model_id: openai/mimo-v2.5
      - model_name: mimo-v2.5-pro
        litellm_model_id: openai/mimo-v2.5-pro
```

The probe script uses the canonical mini7 observation and prints the raw
provider text:

```
XIAOMI_API_KEY=tp-... uv run python scripts/probe_xiaomi_mimo.py mimo-v2.5
XIAOMI_API_KEY=tp-... uv run python scripts/probe_xiaomi_mimo.py mimo-v2.5-pro
```

Observed 2026-05-22: both Mimo models emitted clean unfenced JSON for the
canonical probe. No Chinese preamble, `</think>`, `<start_of_turn>`, or
`<end_of_turn>` normalizer was needed. The raw response envelope does
include a provider `reasoning_content` field, but the adapter parses only
`choices[0].message.content`.

## Recording cassettes (US-051, US-072)

The contract suite under `tests/llm/test_litellm_contract.py` parses
recorded HTTP cassettes for every supported provider. Per US-072 the
goal is for every `canonical_response.yaml` to be a REAL recorded
provider response — synthetic envelopes confirm only that our parser
handles what we wrote, not what the provider actually emits. The
`ProviderCase.synthetic_canonical` flag tracks which providers still
need a real recording; the `test_canonical_response_parses` test skips
those providers so the `live_llm` collection count reflects only
provider-recorded contracts.

### Re-record one provider

Export `PADRINO_RECORD_LLM=1` together with the provider's API-key env
var, delete the existing `canonical_response.yaml` (vcrpy's
`record_mode="once"` only writes when the file is missing), then run the
contract test for that provider:

```
# Cerebras (primary)
rm tests/llm/cassettes/cerebras/canonical_response.yaml
PADRINO_RECORD_LLM=1 CEREBRAS_API_KEY=csk-... \
    uv run pytest tests/llm/test_litellm_contract.py --live-llm \
    -m live_llm -k 'cerebras and canonical'

# DeepInfra (fallback) — DeepSeek-V4-Flash often takes 15+ s on first
# contact, so the fixture extends the adapter timeout to 60 s when
# PADRINO_RECORD_LLM=1; you do not need to bump anything by hand.
rm tests/llm/cassettes/deepinfra/canonical_response.yaml
PADRINO_RECORD_LLM=1 DEEPINFRA_API_KEY=lw... \
    uv run pytest tests/llm/test_litellm_contract.py --live-llm \
    -m live_llm -k 'deepinfra and canonical'

# OpenAI
rm tests/llm/cassettes/openai/canonical_response.yaml
PADRINO_RECORD_LLM=1 OPENAI_API_KEY=sk-... \
    uv run pytest tests/llm/test_litellm_contract.py --live-llm \
    -m live_llm -k 'openai and canonical'

# Xiaomi token-plan (records both Mimo models and their malformed probes)
rm tests/llm/cassettes/xiaomi/mimo_v25_canonical_response.yaml
rm tests/llm/cassettes/xiaomi/mimo_v25_malformed_response.yaml
rm tests/llm/cassettes/xiaomi/mimo_v25_pro_canonical_response.yaml
rm tests/llm/cassettes/xiaomi/mimo_v25_pro_malformed_response.yaml
PADRINO_RECORD_LLM=1 XIAOMI_API_KEY=tp-... \
    uv run pytest tests/llm/test_litellm_contract.py --live-llm \
    -m live_llm -k xiaomi
```

Then flip `synthetic_canonical=False` on the matching `ProviderCase`
row in `tests/llm/test_litellm_contract.py` and re-run the suite to
confirm the recorded cassette replays cleanly:

```
uv run pytest tests/llm/test_litellm_contract.py --live-llm
```

### Verify secrets were scrubbed

The vcrpy hooks `before_record_request` / `before_record_response`
strip `authorization`, `x-api-key`, `api-key`, `cookie`, `set-cookie`,
`openai-organization`, `openai-project`, `anthropic-organization-id`,
and any JSON `api_key` field. After re-recording, grep the cassette
directory for credential-shaped substrings; the audit must return
nothing:

```
grep -rE 'sk-|pk-|csk-|^lw|tp-|Bearer\s' tests/llm/cassettes/ && echo LEAK || echo clean
```

The `test_cassettes_have_no_secret_shaped_substrings` test is the same
audit run inside pytest, and `test_audit_catches_deliberately_leaky_probe`
plants sentinel `sk-...` / `sk-ant-...` / `tp-...` / `Bearer ...`
strings in a tmp cassette to prove the audit's regex set has not silently
gone stale.

### Malformed cassettes

Legacy `malformed_response.yaml` cassettes are intentionally synthetic
where a provider cannot be coaxed into malformed JSON on demand. Xiaomi's
malformed cassettes are real recordings: the provider case uses a
record-only system prompt asking for `oops not json {`, then the replayed
cassette asserts our `coerce_response_failure` path. The test stays in
the parametrize set for every provider.

### When a provider key is unavailable

Per US-072: leave the existing synthetic cassette in place, leave the
`synthetic_canonical=True` flag set with a `TODO(US-072)` comment naming
the missing env var, and the test stays in the parametrize set but
skips so the `live_llm` collection count reflects only provider-recorded
contracts.

The `live_llm` marker is default-skipped (see `tests/conftest.py`); CI
will not run live recordings, so the cassettes you commit are the only
shape the contract suite exercises.

## Prompt customization

The canonical mini7_v1 prompts live under
`src/padrino/llm/prompts/mini7_v1/<role_family>.md` and are seeded by
`padrino bootstrap` (US-052). To experiment with a custom prompt for a
specific agent build:

1. Insert a new `PromptVersion` row with `ruleset_id='mini7_v1'`,
   `version='<your-version-tag>'`, a unique `prompt_hash`, and the new
   `system_prompt` text. The four canonical role families
   (`DECEPTIVE`, `INVESTIGATIVE`, `PROTECTIVE`, `VANILLA_TOWN`) each
   need a row.
2. Insert a new `AgentBuild` pointing at the new `prompt_version_id`.
3. Add the build to a gauntlet roster — the runner picks up the
   per-role prompt via `LiteLlmAdapter.system_prompts_by_role`.

Custom prompts are scoped to the `AgentBuild` they belong to. Ratings
are stamped with the prompt version, so a build run under prompt-v2 and
the same build re-rated under prompt-v3 are two different identities
on the leaderboard. This is intentional — prompt and model are coequal
parts of an agent's identity.

The canonical prompts are read-only inside the package; the seed step
inserts them on a fresh DB but never overwrites custom rows you've
authored. If you delete them, re-running `padrino bootstrap` restores
them (the step is idempotent and only inserts missing role families).

## Verified runbook

The block below is executed by `tests/docs/test_runbooks.py`. It writes
a `providers.yaml` to a sandbox directory and runs `padrino bootstrap
--providers` against it, proving the YAML schema validates and the
secret resolver accepts both supported schemes. Re-running asserts that
the providers step is idempotent (skipped on second invocation).

```bash
# verified
cat > providers.yaml <<'YAML'
providers:
  - name: ollama-local
    auth_secret_ref: env:OLLAMA_API_KEY
    base_url: http://localhost:11434
    default_model: llama3
    timeout_s: 120.0
  - name: openai
    auth_secret_ref: env:OPENAI_API_KEY
    base_url: https://api.openai.com
    default_model: gpt-4o-mini
YAML
OLLAMA_API_KEY=local-dev OPENAI_API_KEY=sk-test-only uv run padrino bootstrap --providers ./providers.yaml > first.json
OLLAMA_API_KEY=local-dev OPENAI_API_KEY=sk-test-only uv run padrino bootstrap --providers ./providers.yaml > second.json
uv run python -c "
import json, pathlib
first = json.loads(pathlib.Path('first.json').read_text())
second = json.loads(pathlib.Path('second.json').read_text())
assert first['succeeded'] and second['succeeded']
first_providers = next(s for s in first['steps'] if s['name'] == 'providers')
second_providers = next(s for s in second['steps'] if s['name'] == 'providers')
assert sorted(first_providers['detail']['inserted']) == ['ollama-local', 'openai']
assert second_providers['detail']['inserted'] == []
assert sorted(second_providers['detail']['skipped']) == ['ollama-local', 'openai']
print('providers step is idempotent')
"
```
