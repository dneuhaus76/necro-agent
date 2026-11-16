# NECRO – Provider Profiles and Model Probe

OpenAI-compatible endpoints differ in a few fields. NECRO keeps these
differences in one table (`modules/core/providers.py`) instead of hostname
checks in the HTTP layer. A new provider is one row, not edits in three
files. The model probe (`modules/core/probe.py`) reads the server's model
details and feeds `/model`.

## Profiles (modules/core/providers.py)

A `Profile` is a `NamedTuple` with 6 fields:

| Field | Meaning |
|---|---|
| `hosts` | hostname suffixes that select this profile |
| `thinking` | request field for thinking: `bool` \| `struct` \| `omit` |
| `info_api` | model-details API (`""` = OpenAI `/models`, `google` = native) |
| `key_header` | API key header on the native API (`""` = Bearer) |
| `detail` | rows that `/model` shows for this provider |
| `notes` | free text |

Three profiles exist:

| Profile | Hosts | Thinking field | Notes |
|---|---|---|---|
| `openai` (default) | everything else | `bool` | plain `/chat/completions` + `/models` (llama.cpp, vLLM, OpenRouter, ...) |
| `google` | `googleapis.com` | `omit` (the field is rejected with HTTP 400) | no `/models` on the OpenAI endpoint; native API lookup |
| `deepseek` | `deepseek.com` | `struct` (`{"type": "enabled"}`) | hosted, flat `/models` fields |

### Resolution (`providers.resolve`)

Priority: explicit override, then hostname suffix match, then the OpenAI
default.

- **cfg `providers`** – dict `{base url or hostname: provider name}`. Needed
  behind a proxy that hides the real host. Invalid entries are dropped
  (a typo must not break the probe).
- **env `NECRO_PROVIDERS`** – `host=name;host=name` (the env twin of cfg
  `providers`).
- **Hostname suffix** – `googleapis.com`, `deepseek.com`.

### Thinking field (`providers.thinking_field`)

The request field value for a given profile: `bool` (default), a struct
(DeepSeek), or `None` = omit the field entirely (Google).

### Native headers (`providers.native_headers`)

Chat requests use Bearer auth everywhere. Only the native lookups (model
details) use the provider's key header (e.g. `x-goog-api-key`). The key
travels in the header, never in the URL (query strings end up in logs).

## Model Probe (modules/core/probe.py)

`fetch_model_info(base, model, ctx_fallback, token, overrides)` runs at
startup (parallel with thinking detection, `probe.resolve`). It returns a
dict with: context window, reasoning effort, status, sampling parameters,
architecture, file, GPU layers, threads, KV cache, created date.

Lookup order:

1. **Native API** – when the profile has `info_api` (Google:
   `GET /v1beta/models/<model>`, key header, OAuth token retried as Bearer
   on 401/403).
2. **OpenAI `/models`** – llama.cpp model_manager args (`--ctx-size`,
   `--temperature`, ...) or flat hosted fields (`context_window`, `effort`).
3. **Hosted fallback** – minimal info (`status: hosted`, `arch:
   text->text`) when no lookup works. A reason string marks a failed
   lookup.

`detect_thinking(base, model, token)` sends one 16-token request and checks
for `reasoning_content`. Providers that reject the `thinking` field are
probed without it.

## The `/model` Command

- `/model` – shows the model details (`probe.info_text`). The provider
  profile decides the rows (`Profile.detail`): a hosted API has no GPU
  layers, threads or KV cache, so those rows are left out instead of "-".
- `/model <name>` – switches the live model. The context window re-probes;
  the `ctx_models` cap re-applies (see startparams.md).

The context window shown is the effective value (server value, capped by
`ctx_models`), not the raw server number.
