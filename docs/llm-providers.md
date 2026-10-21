# LLM Provider Options

Provider choice for the agentic PR reviewer. LangChain/LangGraph are provider-agnostic —
the model is a config string (`init_chat_model("provider:model")`), so this decision is
fully reversible at any time. See design §1251 and §1850–1854.

## Options (prices per 1M tokens, as of Oct 2026 — verify before committing)

| Model | Provider | Input | Output | Free tier | Verdict |
|---|---|---|---|---|---|
| gemini-3.8-flash | Google | $0.75 | $3.75 | ✅ ~1,500 req/day (10 RPM, 250K tok/min), no expiry, no card | **Recommended for PoC dev** |
| gpt-5.6-luna | OpenAI | $0.20 | $1.20 | ❌ ($5 signup credits, expires ~3 months) | Cheapest paid; best schema enforcement |
| gpt-5.4-mini | OpenAI | $0.75 | $4.50 | ❌ | Quality step-up from Luna |
| claude-haiku-4.5 | Anthropic | $1.00 | $5.00 | ❌ ($5 signup credits) | Safest tool-calling if agent loops get flaky |
| gemini-3.5-flash-lite | Google | $0.30 | $2.50 | ✅ | Diff summarization only; weak on subtle bugs |
| deepseek-v4 | DeepSeek | $0.14 | $0.28 | ⚠️ limited | Avoid for agent loops — unreliable tool-calling |
| claude-sonnet-5 | Anthropic | $2.00 | $10.00 | ❌ | When review quality matters (prod tier) |

## Recommendation

- **Development:** `google_genai:gemini-3.8-flash` on the free tier — covers the whole PoC at $0.
- **Demos / paid tier:** `openai:gpt-5.6-luna` ($0.03–0.10 per PR review) — cheapest solid option.
- **If loops get flaky** (malformed JSON, misfired tool calls): climb one rung to `claude-haiku-4.5`.
- **Future quality tier** for the reviewer/judge split (design §1854): Gemini Flash for the cheap
  first pass + GPT or Claude for judging — the split works across providers, not just models.

## Cost reference

- Typical PR review pass: ~50–150K input (diff + file context) + a few K structured
  findings output.
- Gemini 3.8 Flash free tier: $0 (within 1,500 req/day).
- GPT-5.6 Luna: ~$0.03–0.10 per PR.
- Claude Sonnet 5: ~$0.10–0.35 per PR.
- Prompt caching helps all providers: repeated system prompt + repo context bills at
  ~10% of input price on cache hits (OpenAI cached input $0.02/M on Luna).
