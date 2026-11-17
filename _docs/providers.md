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

Four profiles exist:

| Profile | Hosts | Thinking field | Notes |
|---|---|---|---|
| `openai` (default) | everything else | `bool` | plain `/chat/completions` + `/models` (llama.cpp, vLLM, OpenRouter, ...) |
| `google` | `googleapis.com` | `omit` (the field is rejected with HTTP 400) | no `/models` on the OpenAI endpoint; native API lookup |
| `deepseek` | `deepseek.com` | `struct` (`{"type": "enabled"}`) | hosted, flat `/models` fields |
| `bigmoe` | none (name it) | `bool` | BigMoeOnEdge runner: model details on `/api/info` |

### Resolution (`providers.resolve`)

Priority: explicit name, then hostname suffix match, then the OpenAI default.
One way to name a runner - no separate override table.

- **cfg `provider`** – runner of the main endpoint (`base_url`). Needed only
  when the hostname gives no hint: a local IP or a proxy.
- **cfg `swarm[].provider`** – the same for a swarm server. Each entry names
  its own runner, so a BigMoeOnEdge agent and a llama.cpp agent can sit in
  one list.
- **env `NECRO_PROVIDER`** – wins over cfg `provider` for the main endpoint.
- **Hostname suffix** – `googleapis.com`, `deepseek.com`.

A wrong name is ignored (a typo must not break startup); the hostname then
decides. `providers.name_of` normalises the value (`BigMoe` -> `bigmoe`).

Naming is optional: when no name is given and `/models` publishes no server
details, the probe asks the runner's own `/api/info` (BigMoeOnEdge). A local
IP therefore needs no `provider` key at all. An explicit name skips that probe.

### Thinking field (`providers.thinking_field`)

The request field value for a given profile: `bool` (default), a struct
(DeepSeek), or `None` = omit the field entirely (Google).

### Native headers (`providers.native_headers`)

Chat requests use Bearer auth everywhere. Only the native lookups (model
details) use the provider's key header (e.g. `x-goog-api-key`). The key
travels in the header, never in the URL (query strings end up in logs).

## Model Probe (modules/core/probe.py)

`fetch_model_info(base, model, ctx_fallback, token, name)` runs at
startup (parallel with thinking detection, `probe.resolve`). It returns a
dict with: context window, reasoning effort, status, sampling parameters,
architecture, file, GPU layers, threads, KV cache, created date.

Lookup order:

1. **Native API** – when the profile has `info_api` (Google:
   `GET /v1beta/models/<model>`, key header, OAuth token retried as Bearer
   on 401/403).
2. **OpenAI `/models`** – llama.cpp model_manager args (`--ctx-size`,
   `--temperature`, ...) or flat hosted fields (`context_window`, `effort`).
3. **Runner `/api/info`** – only when step 2 gave no details and no provider
   name was set (BigMoeOnEdge): name, arch, `n_ctx`, threads, load time.
4. **Hosted fallback** – minimal info (`status: hosted`, `arch:
   text->text`) when no lookup works. A reason string marks a failed
   lookup.

`detect_thinking(base, model, token)` sends one 16-token request and checks
for `reasoning_content`. Providers that reject the `thinking` field are
probed without it.

The reported **effort** is the server truth from the launch args
(`_reasoning`): `--chat-template-kwargs '{"reasoning_effort":"low"}'` or
`--reasoning-effort` win, then `--reasoning` and `--reasoning-budget`.
`--reasoning false` or `--reasoning-budget 0` means `off` – such a preset
emits no reasoning, whatever the request asks for. No such flag means
unknown (`""`), and the display falls back to the request toggle.

## The `/model` Command

- `/model` – shows the model details (`probe.info_text`). The provider
  profile decides the rows (`Profile.detail`): a hosted API has no GPU
  layers, threads or KV cache, so those rows are left out instead of "-".
- `/model <name>` – switches the live model. The context window re-probes;
  the `ctx_models` cap re-applies (see startparams.md).

The context window shown is the effective value (server value, capped by
`ctx_models`), not the raw server number.
