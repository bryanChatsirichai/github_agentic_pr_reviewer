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

## Switching providers (env config)

`agents/llm.py` resolves the model from env — switching providers is a one-value
change, locally and in CI. `MODEL_PROVIDER` selects one of:

| `MODEL_PROVIDER` | Key env (required) | Model env (optional) | Default model |
|---|---|---|---|
| `google` | `GOOGLE_API_KEY` | `GOOGLE_MODEL` | `gemini-3.5-flash-lite` |
| `openai` | `OPENAI_API_KEY` | `OPENAI_MODEL` | `gpt-5.6-luna` |
| `claude` (`anthropic` alias) | `ANTHROPIC_API_KEY` | `CLAUDE_MODEL` | `claude-haiku-4.5` |

Keep all three keys configured (in `.env` locally, as repo *secrets* in CI) and
flipping `MODEL_PROVIDER` (a repo *variable* in CI, or a `model_provider` input
for caller repos) is the only change needed. The legacy `AI_API_KEY` +
`MODEL_NAME` ("provider:model") pair still works when `MODEL_PROVIDER` is unset.

## Cost reference

- Typical PR review pass: ~50–150K input (diff + file context) + a few K structured
  findings output.
- Gemini 3.8 Flash free tier: $0 (within 1,500 req/day).
- GPT-5.6 Luna: ~$0.03–0.10 per PR.
- Claude Sonnet 5: ~$0.10–0.35 per PR.
- Prompt caching helps all providers: repeated system prompt + repo context bills at
  ~10% of input price on cache hits (OpenAI cached input $0.02/M on Luna).
