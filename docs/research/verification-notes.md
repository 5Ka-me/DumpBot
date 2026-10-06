# Fact-check notes (2026-10-06)

Corrections that came out of the research fact-check pass and matter for the implementation:

- **chutes cost formula.** `usage.prompt_tokens` already includes cached tokens. Correct cost:
  `(prompt_tokens - cached_tokens) * price_in + cached_tokens * price_cache_read + completion_tokens * price_out`
  (all per 1M tokens). `cached_tokens` comes from `usage.prompt_tokens_details.cached_tokens` when present.
  `/v1/models` exposes `pricing.prompt`, `pricing.completion` and `pricing.input_cache_read` (USD per 1M).
- **`GET https://llm.chutes.ai/v1/models` is public.** It returns 200 without an `Authorization` header but **401 with an invalid Bearer**.
  Fetch prices without the auth header so that a bad key does not also break price lookup.
- **llm.chutes.ai** answers an anonymous `POST /v1/chat/completions` with 429 (HTML). Unknown paths return a 404 HTML page.
  Chute hosts (`*.chutes.ai`) return 401 `{"detail":"Authentication required."}` for any path when no key is sent.
- **Gemma 4 on chutes** only reasons when the `<|think|>` token starts the system prompt, so thinking is off by default.
  chutes also mirrors `chat_template_kwargs.enable_thinking` to `thinking`.
- **Not yet verified, needs a smoke test with the real key:**
  - `response_format: json_schema` / `json_object` acceptance per model
  - whether the STT chute accepts raw OGG
- **`set_my_commands`:** a list set with `language_code="ru"` is shown only to Telegram clients whose language is Russian.
  Set the list without `language_code`.
- **aiogram yanks:** only 3.29.0 was yanked. This does not matter here; we pin 3.31.0.
- **SQLAlchemy 2.1:** upgrading is mostly a pin change, but autoflush becomes unconditional. Run the full test suite before switching.
- **Compose `env_file`:** values are interpolated (`${VAR}` works in unquoted and double-quoted values).
