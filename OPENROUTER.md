# OpenRouter free models

Configure the local `.env` file:

```dotenv
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=your-local-api-key
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=openrouter/free
```

Restart the service after changing configuration. Existing provider credentials remain available; change the provider setting back to restore the previous provider. Never commit API keys.

The free router selects an available model supporting the requested structured output or tool calling. It is not a fixed model. Validate analysis results; a health endpoint reporting that credentials are configured is not proof of successful inference.

All services share the account's free-model request allowance. The deployment key reported a limit of 50 requests per UTC day on 2026-09-21. Read `free_model_daily_requests` from `GET /api/v1/key` for the current allowance. Large backlogs require multiple days or another provider.

Line Bot's `/AI` retains its configured `AI_FALLBACK_PROVIDERS`; the stock Agent selects `AI_PROVIDER` directly. Tracker services use the selected provider and keep their existing failed-analysis retry behavior. Switching providers does not resolve LINE message delivery quotas.

References:
- https://openrouter.ai/openrouter/free
- https://openrouter.ai/docs/api/reference/limits
