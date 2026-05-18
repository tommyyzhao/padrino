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

The `default_model` field is forwarded to LiteLLM. The full model id
that the adapter actually sends is `<provider>/<model>`:

| Provider     | Example identifier                                |
|--------------|---------------------------------------------------|
| OpenAI       | `openai/gpt-4o-mini`                              |
| Anthropic    | `anthropic/claude-haiku-4-5`                      |
| Cerebras     | `cerebras/zai-glm-4.7`                            |
| DeepInfra    | `deepinfra/deepseek-ai/DeepSeek-V4-Flash`         |
| Groq         | `groq/llama-3.1-70b-versatile`                    |
| Mistral      | `mistral/mistral-large-latest`                    |
| Ollama       | `ollama/llama3` (note: dispatches to `/api/generate`, not `/api/chat`) |

If LiteLLM picks the wrong endpoint shape for your provider (some
upstreams disagree about whether to use OpenAI-compat `/chat/completions`
vs. their native chat format), set `base_url` on the `ModelProvider`
row to pin the override.

## Recording cassettes (US-051)

The contract suite under `tests/llm/test_litellm_contract.py` parses
recorded HTTP cassettes for every supported provider. Adding a new
provider means either:

1. Hand-author two YAML cassettes (canonical + malformed response) per
   the existing pattern under `tests/llm/cassettes/<provider>/`. The
   audit test `test_cassettes_have_no_secret_shaped_substrings` scans
   committed files for `sk-`, `sk-ant-`, and `Bearer <token>` patterns;
   never embed a real key.
2. Re-record with a real key by exporting `PADRINO_RECORD_LLM=1` and
   running the contract test once:

   ```
   PADRINO_RECORD_LLM=1 OPENAI_API_KEY=sk-... \
       uv run pytest -m live_llm --live-llm tests/llm/test_litellm_contract.py
   ```

   The vcrpy fixture is configured with `record_mode="once"` while the
   env var is set, so cassettes are written if they don't exist and
   replayed thereafter. Scrub the resulting YAML before committing —
   `before_record_request` / `before_record_response` strip auth-shaped
   headers and `api_key` JSON fields, but the audit test is the safety
   net you should not rely on bypassing.

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
