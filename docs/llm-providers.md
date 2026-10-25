# LLM Provider Options

Provider choice for the agentic PR reviewer. LangChain/LangGraph are provider-agnostic —
the model is a config string (`init_chat_model("provider:model")`), so this decision is
fully reversible at any time. See design §1251 and §1850–1854.

## Options (prices per 1M tokens, as of Oct 2026 — verify before committing)

| Model | Provider | Input | Output | Free tier | Verdict |
|---|---|---|---|---|---|
| gemini-3.8-flash | Google | $0.75 | $3.75 | ✅ ~20 req/day (thinking model; ~50s–2min/call, ignores format instructions) | Good quality, too slow/flaky for dev loop |
| gpt-5.6-luna | OpenAI | $0.20 | $1.20 | ❌ ($5 signup credits, expires ~3 months) | Cheapest paid; best schema enforcement |
| gpt-5.4-mini | OpenAI | $0.75 | $4.50 | ❌ | Quality step-up from Luna |
| claude-haiku-4.5 | Anthropic | $1.00 | $5.00 | ❌ ($5 signup credits) | Safest tool-calling if agent loops get flaky |
| gemini-3.5-flash-lite | Google | $0.30 | $2.50 | ✅ ~500 req/day (fast: ~5s/call, follows format instructions) | **Recommended for PoC dev** |
| deepseek-v4 | DeepSeek | $0.14 | $0.28 | ⚠️ limited | Avoid for agent loops — unreliable tool-calling |
| claude-sonnet-5 | Anthropic | $2.00 | $10.00 | ❌ | When review quality matters (prod tier) |

## Recommendation

- **Development:** `google_genai:gemini-3.5-flash-lite` on the free tier — fast (~5s/call), follows format instructions, ~500 req/day covers the whole PoC at $0. Verified too slow on 3.8-flash free tier: 50s–2min/call (thinking) and it ignored the sanity-check prompt.
- **Demos / paid tier:** `openai:gpt-5.6-luna` ($0.03–0.10 per PR review) — cheapest solid option.
- **If loops get flaky** (malformed JSON, misfired tool calls): climb one rung to `claude-haiku-4.5`.
- **Future quality tier** for the reviewer/judge split (design §1854): Gemini Flash for the cheap
  first pass + GPT or Claude for judging — the split works across providers, not just models.

## Gateway option: OpenCode Zen (evaluated Oct 2026, not wired up)

[OpenCode Zen](https://opencode.ai/docs/zen/) is a pay-as-you-go gateway: **one API key**
covers Google, OpenAI and Anthropic models. It proxies each provider's *native* API rather
than normalizing to one format, so the existing LangChain classes still apply — only the
`base_url` and key change:

| Provider | Zen endpoint | LangChain class |
|---|---|---|
| OpenAI | `https://opencode.ai/inference/openai/v1/responses` | `ChatOpenAI` (Responses API required for GPT models) |
| Anthropic | `https://opencode.ai/inference/anthropic/v1/messages` | `ChatAnthropic` |
| Google | `https://opencode.ai/inference/google/v1beta/models/<id>` | `ChatGoogleGenerativeAI` (custom endpoint — least certain) |

Untested: the LangChain wiring above has not been run against Zen.

- **Pricing:** matches the table above for flash-lite, Luna, gpt-5.4-mini, Haiku 4.5 and
  Sonnet 5, plus a **4.6% + $0.31 fee per credit top-up**. Zen lists gemini-3.8-flash at
  $1.50 / $7.50 — double the table's figure; unclear whether that is a Zen markup or the
  table is stale, so check Google's direct price.
- **No free tier for our use:** Zen's free models are restricted to the OpenCode client, and
  Gemini via Zen is paid — so it **does not replace the Google free tier for dev**.
- **Data retention:** Gemini/DeepSeek zero-retention; OpenAI/Anthropic requests retained 30 days.
- **Prompt caching** through the proxy is undocumented — the cost reference below assumes it works.
- **OpenCode Go** ($10/mo subscription, separate key) is **not suitable**: built for interactive
  coding agents, traffic is monitored for abuse, requires an `x-opencode-session` header, and
  doesn't include gemini-3.5-flash-lite.

**Verdict:** viable as a single-key convenience for paid runs (would be a fourth
`MODEL_PROVIDER`), not a cost saver. Keep Google direct for dev.

**Newer models seen on Zen, worth evaluating** (Zen prices; verify direct prices first):
- `claude-haiku-5-5`: $0.10 / $0.50 (≤100K input), $0.50 / $2.50 above — a typical review
  (50–150K input) can cross the tier. Far cheaper than `claude-haiku-4.5` if it holds.
- `gpt-6-luna`: $0.10 / $0.50 (≤272K input) — half the price of `gpt-5.6-luna`.

## Switching providers (env config)

`agents/llm.py` resolves the model from env — switching providers is a one-value
change, locally and in CI. `MODEL_PROVIDER` selects one of:

| `MODEL_PROVIDER` | Key env (required) | Model env (optional) | Default model |
|---|---|---|---|
| `google` | `GOOGLE_API_KEY` | `GOOGLE_MODEL` | `gemini-3.5-flash-lite` |
| `openai` | `OPENAI_API_KEY` | `OPENAI_MODEL` | `gpt-5.6-luna` |
| `claude` | `ANTHROPIC_API_KEY` | `CLAUDE_MODEL` | `claude-haiku-4.5` |

`MODEL_PROVIDER` is **required** (case-insensitive, no aliases): if it is unset
or unknown, the run logs the error and exits 1 — locally and in CI alike. Keep
all three keys configured (in `.env` locally, as repo *secrets* in CI) and
flipping `MODEL_PROVIDER` (a repo *variable* in CI, or a `model_provider` input
for caller repos) is the only change needed.

## Cost reference

- Typical PR review pass: ~50–150K input (diff + file context) + a few K structured
  findings output.
- Gemini 3.8 Flash free tier: $0 (within 1,500 req/day).
- GPT-5.6 Luna: ~$0.03–0.10 per PR.
- Claude Sonnet 5: ~$0.10–0.35 per PR.
- Prompt caching helps all providers: repeated system prompt + repo context bills at
  ~10% of input price on cache hits (OpenAI cached input $0.02/M on Luna).
