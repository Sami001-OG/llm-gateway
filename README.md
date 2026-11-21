# LLM Gateway — one API, many accounts

A self-hosted API gateway that pools **50–100+ AI accounts** behind a single
endpoint. It rotates between accounts and cools down ones that hit rate limits
or quota walls, so one exhausted account doesn't stop your workflow.

It speaks all three wire formats used by current coding agents:

| Endpoint | Format | Works with |
|---|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions | OpenAI SDK, OpenCode, most frameworks |
| `POST /v1/responses` | OpenAI Responses | **Codex** (current releases require this) |
| `POST /v1/messages` | Anthropic Messages | **Claude Code**, Anthropic SDK |
| `GET /v1/models` | both | model discovery |
| `POST /v1/messages/count_tokens` | Anthropic | token counting |
| `/admin/*` | JSON | stats, key control, hot reload |

Requests aimed at the other provider are translated transparently (including
streaming, tools, system prompts, and images). `/v1/responses` is a native
pass-through and therefore only routes to OpenAI pools.

> **Responsible use:** Only pool accounts you are authorized to use, and comply
> with each provider's terms. See the [Compliance](#compliance-whats-allowed-and-what-isnt)
> section — the distinction between **API keys** and **subscription/Claude.ai
> login tokens** is critical.

---

## Web dashboard

A built-in, dependency-free control center is available at **`http://<host>:<port>/`**
(or `/ui`). Open it and enter an **admin token** from `server.admin_tokens`.

<details>
<summary>What you can do there</summary>

- **Overview** — live counters (accounts, available, cooling down, requests), pool
  health bars, and a route summary.
- **Accounts** — pick a pool, paste dozens/hundreds of keys at once, drag rows or
  use ↑ ↓ to set priority, disable/enable individual accounts, remove them, and
  see per-key success/failure counts and cooldown state.
- **Endpoints** — create and edit model aliases. Each alias maps to one pool or
  an **ordered failover list** across pools, with an optional upstream model
  override. This is where you "select accounts for the endpoint".
- **API Tokens** — generate (or supply) client tokens and admin tokens, copy the
  newly created token once, and revoke existing ones by ID.
- **Settings** — bind host, port, timeout, retries, selection strategy, and all
  cooldown values. Saved changes are written to `config.yaml` and applied
  immediately (host/port need a restart).
- **Remote access** — ready-to-copy base URLs and agent snippets for this device.

Every change is persisted atomically to `config.yaml` and hot-applied — no restart
needed. Per-account statistics survive edits.

> The HTML shell itself is public, but **all data lives behind admin-token-protected
> endpoints**. The token is kept in your browser's localStorage. Never expose the
> dashboard publicly without a strong admin token and TLS.

</details>

## Verified compatibility (checked Nov 2026)

| Item | Finding |
|---|---|
| Current OpenAI models | `gpt-6.2-astra`, `gpt-6.2-sol`, `gpt-6.1-terra`, `gpt-6-luna` |
| Current Claude models | `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-fable-5-2` |
| Retired Claude models | `claude-sonnet-4-20250514`, `claude-3-5-sonnet-*`, `claude-3-5-haiku-*` — **removed**, would 404 |
| GPT-4o / GPT-4.1 | Deprecated, shut down **2027-01-14**; `gpt-3.5-turbo`/`gpt-4`/`o1` already shut down |
| Chat Completions | Still supported by GPT-6 models, but tool calling needs Responses API |
| Codex | Current releases **reject** Chat-Completions-only gateways — needs `/v1/responses` (now implemented) |
| Claude Code | Sends `POST /v1/messages?beta=true` with `anthropic-beta`, `anthropic-version`, `thinking`, `tools`, `output_config`; gateway forwards these unchanged |
| Claude sampling params | `temperature`/`top_p`/`top_k` removed in Anthropic API `2026-10-21` and rejected on Claude 4.7+ — gateway now strips them |
| Rate limit scope | **Organization/project level, not per API key** — see the important caveat below |

### ⚠️ The most important caveat

Both providers enforce rate limits at the **organization/workspace** level:

- OpenAI: "Rate limits are defined at the organization level and at the project
  level, not user level." Multiple keys in the same project share one quota.
- Anthropic: "The API enforces service-configured limits at the organization
  level." Workspaces can have sub-limits but cannot exceed the org limit.

**Therefore: 100 API keys from one OpenAI organization will NOT give you 100×
throughput.** They all draw from the same RPM/TPM bucket. Rotation only helps
when your keys belong to **independent organizations / billing accounts**
(e.g. separate API Console orgs, separate workspaces with their own quotas, or
different providers). If every key is from one org, use tier upgrades or the
Batch/Background APIs instead — this gateway cannot multiply a single quota.

---

## Install

```bash
cd /data/data/com.termux/files/home/llm-gateway
python3 -m pip install -r requirements.txt
```

On Termux, if `pip` hangs, point its temp dir somewhere writable:

```bash
mkdir -p /data/data/com.termux/files/usr/tmp/opencode/tmp
export TMPDIR=/data/data/com.termux/files/usr/tmp/opencode/tmp
python3 -m pip install -r requirements.txt
```

## Configure

```bash
cp config.example.yaml config.yaml
nano config.yaml
python3 run.py --config config.yaml
```

Check it:

```bash
curl http://localhost:8000/health -H "Authorization: Bearer change-me-client-token"
```

### Adding 50–100+ accounts

```yaml
pools:
  openai_main:
    provider: openai
    base_url: https://api.openai.com/v1
    models: [gpt-6.2-astra, gpt-6.2-sol, gpt-6-luna]
    keys:
      - ${OPENAI_KEY_1}
      - file:/etc/secrets/openai_keys.txt   # one key per line
```

Make sure those keys are from distinct organizations if your goal is more
throughput. Hot-reload changes with `POST /admin/reload`.

---

## Wiring up AI agents

Replace `http://localhost:8000` with your gateway address and
`change-me-client-token` with a value from `server.auth_tokens`.

### Claude Code

```bash
export ANTHROPIC_BASE_URL="http://localhost:8000"      # no /v1 suffix!
export ANTHROPIC_AUTH_TOKEN="change-me-client-token"   # sent as Bearer
# or: export ANTHROPIC_API_KEY="change-me-client-token" # sent as x-api-key
export ANTHROPIC_MODEL="claude-opus-5-5"               # default Claude Code model
claude
```

`claude-opus-5-5` is included in the default Anthropic pool, so it resolves
even without `ANTHROPIC_MODEL`. The gateway forwards `anthropic-beta` and
`anthropic-version` verbatim, keeps `thinking`/`tools`/`output_config` intact
for Anthropic upstreams, and returns `retry-after` / `anthropic-ratelimit-*`
headers so Claude Code's stall detection and usage display work.

### OpenCode

`~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "gateway": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "My Gateway",
      "options": {
        "baseURL": "http://localhost:8000/v1",
        "apiKey": "change-me-client-token"
      },
      "models": {
        "gpt-6.2-sol": { "name": "GPT-6.2 Sol (pool)" },
        "claude-sonnet-5-5": { "name": "Claude Sonnet 5.5 (pool)" },
        "auto": { "name": "Auto / all accounts" }
      }
    }
  }
}
```

### Codex

Current Codex releases do **not** support `wire_api = "chat"`; use the
Responses endpoint:

```toml
model_provider = "gateway"
model = "gpt-6.2-sol"

[model_providers.gateway]
name = "gateway"
base_url = "http://localhost:8000/v1"
env_key = "CODEX_GATEWAY_KEY"
wire_api = "responses"
```

```bash
export CODEX_GATEWAY_KEY="change-me-client-token"
codex
```

### Any OpenAI-compatible client

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="change-me-client-token")
print(client.chat.completions.create(
    model="auto", messages=[{"role": "user", "content": "Hello"}]
).choices[0].message.content)
```

---

## How rotation works

1. Routes resolve the requested model to one or more pools.
2. The pool picks an available account (`least_used` by default).
3. The call is forwarded (with translation if the client/provider formats differ).
4. On 429 → cooldown honoring `Retry-After`; quota/billing → long cooldown;
   401/403 → disable; 5xx → exponential backoff.
5. The request retries with a different account, up to `max_retries`.

Admin API (needs an `admin_tokens` value):

```bash
curl http://localhost:8000/admin/stats -H "Authorization: Bearer change-me-admin-token"
curl http://localhost:8000/admin/keys  -H "Authorization: Bearer change-me-admin-token"
curl -X POST 'http://localhost:8000/admin/keys/openai_main/openai_main%230/cooldown' \
  -H "Authorization: Bearer change-me-admin-token" -H 'Content-Type: application/json' \
  -d '{"seconds":60,"reason":"maintenance"}'
curl -X POST 'http://localhost:8000/admin/keys/openai_main/openai_main%230/enable' \
  -H "Authorization: Bearer change-me-admin-token"
curl -X POST http://localhost:8000/admin/reload \
  -H "Authorization: Bearer change-me-admin-token"
```

---

## Compliance: what's allowed and what isn't

This is the part that determines whether your setup is safe. Based on the
current published terms:

**Generally fine**
- Holding multiple accounts you personally own, each used by you.
- Using **API keys** issued by OpenAI / Anthropic, billed to their owner, in
  your own tools. Anthropic's Claude Code legal page explicitly permits
  customers to provision and manage their own API keys in development
  environments/secrets managers for authorized users.
- Routing requests through a gateway you operate, when the credential is an
  API key billed per token to its owner.

**Prohibited or high-risk — do not use these tokens in the gateway**
- **ChatGPT / Claude subscription login tokens, session tokens, or OAuth
  credentials** in third-party harnesses. Anthropic: "developers may not
  collect, store, or intermediate Claude.ai credentials or session tokens."
  OpenAI's Sign-in-with-ChatGPT terms forbid "creating multiple accounts,
  splitting usage, rotating accounts, or otherwise bypassing usage limits" and
  "pooling, transferring, reselling, gifting, or sharing ChatGPT plan usage."
- Sharing one account/API key between multiple people, or reselling/renting
  access to accounts.
- Creating accounts to evade detection, bans, guardrails, or supported-region
  checks. Both Usage Policies name this explicitly.
- Automatically creating accounts to obtain more quota.

**Grey area — check with the provider**
- Owning several personal subscriptions solely to get more usage. Neither
  company's terms clearly permit *automated* routing across them for that
  purpose, and OpenAI prohibits "circumvent any rate limits or restrictions."
  Ask support before relying on it.

If you are unsure, prefer the sanctioned routes: raise your API tier, request
higher limits, add usage credits, or use Batch/Background APIs. This tool is a
legitimate load balancer, not a way around provider enforcement.

---

## Known limitations

- `/v1/responses` routes only to OpenAI pools (no Responses↔Messages translation).
- Translated streams (OpenAI↔Anthropic) do not emit SSE `ping` events during
  long pauses; native Anthropic-to-Anthropic pass-through does forward them.
- If a provider enforces limits per source IP, keys from different orgs on one
  IP may still collide — that requires proxy rotation at the network layer.
- `temperature`, `top_p`, `top_k` are dropped for Anthropic upstreams by design.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

24 integration tests using a local mock upstream — no real keys or network
needed. Covers rotation, cooldowns, all three endpoints, streaming, tool
translation, auth, admin API, web dashboard API, and config expansion.

## Sources

- OpenAI: [rate limits](https://developers.openai.com/api/docs/guides/rate-limits), [deprecations](https://developers.openai.com/api/docs/deprecations), [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), [Services Agreement](https://openai.com/policies/services-agreement), [Sign-in with ChatGPT terms](https://openai.com/policies/sign-in-with-chatgpt-terms/), [Codex models](https://developers.openai.com/codex/models)
- Anthropic: [rate limits](https://platform.claude.com/docs/en/api/rate-limits), [model deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations), [Claude Code gateway compatibility](https://code.claude.com/docs/en/llm-gateway-protocol.md), [legal and compliance](https://code.claude.com/docs/en/legal-and-compliance), [Usage Policy](https://www.anthropic.com/legal/aup), [Consumer Terms](https://www.anthropic.com/legal/consumer-terms)
