# sub2api - OpenAI Responses provider

This provider talks to a gateway that exposes the OpenAI Responses API while
serving an Anthropic model group.

Required environment variable:

```dotenv
SUB2API_API_KEY=...
```

Optional overrides:

```dotenv
SUB2API_BASE_URL=http://172.16.3.6:8589/v1
SUB2API_MODEL=deepseek-v4-flash
SUB2API_TIMEOUT=60
SUB2API_MAX_RETRIES=3
SUB2API_DISABLE_RESPONSE_STORAGE=true
```

Select it with:

```dotenv
AGENT_MODEL=sub2api
```

Non-secret defaults live in `config/plugins/model/sub2api.json`. The API key
must stay in `.env` or the deployment platform's secret store.
