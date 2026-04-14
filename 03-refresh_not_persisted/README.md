# Refreshed OAuth2 credentials are not persisted

Demonstrates that `ToolAuthHandler._get_existing_credential()` refreshes OAuth2 credentials in memory but never writes the refreshed credential back to the credential store. On the next tool invocation, the store returns the stale pre-refresh credential; ADK tries to refresh with the now-rotated refresh_token; the provider rejects it; the user is prompted for full re-authorization.

## Files

Structure mirrors [`contributing/samples/oauth2_client_credentials`](https://github.com/google/adk-python/tree/main/contributing/samples/oauth2_client_credentials):

- **`agent.py`** — `LlmAgent` with `OpenAPIToolset` pointed at the test server's `/api/weather` endpoint. Uses `authorization_code` flow (not `client_credentials`) because the bug lives in the refresh code path, which `client_credentials` doesn't exercise.
- **`main.py`** — starts the local OAuth2 test server, seeds a real credential into session state, and runs the agent against two weather queries in the same session via `InMemoryRunner`.
- **`oauth2_test_server.py`** — adapted from the ADK sample's test server; adds `refresh_token` grant handling with token rotation.

## Prerequisites

- `gcloud auth application-default login` or other Vertex AI credentials
- `.env` file in this directory with:
  ```
  GOOGLE_GENAI_USE_VERTEXAI=TRUE
  GOOGLE_CLOUD_PROJECT=<your-project>
  GOOGLE_CLOUD_LOCATION=<region>
  ```

## Run

From this directory:

```bash
# Reproduce the bug (default)
uv run python main.py

# Run with the proposed upstream fix applied
uv run python main.py --apply-fix
```

The `--apply-fix` flag monkey-patches `ToolAuthHandler._get_existing_credential` to call `self._store_credential(refreshed)` after a successful refresh — the one-line change proposed in the issue. With the fix applied, both queries succeed. Without it, the second query triggers re-auth.

## Expected output

```
🔑 Seeded credential: access_token='<seed>'… (expires_at=1)

👤 User: What's the weather in Tokyo?
🌤️  Weather Assistant:
    [event: function_call get_weather by WeatherAssistant]
    [event: function_response get_weather by WeatherAssistant]
The weather in Tokyo is Sunny ...

👤 User: And the weather in London?
🌤️  Weather Assistant:
    [event: function_call get_weather by WeatherAssistant]
    [event: function_call adk_request_credential by WeatherAssistant]
    [event: function_response get_weather by WeatherAssistant]
I'm sorry, I cannot fulfill this request. The tool requires authorization...
```

**Tokyo query:** tool succeeds — ADK's in-memory refresh of the expired seed credential worked. But the refreshed tokens were never written back to session state.

**London query:** tool call fails — ADK finds the stale credential in state, attempts to refresh with the original (now-rotated) refresh_token, the test server rejects it, and ADK falls through to `_request_credential()`. The user is prompted to re-authorize, mid-session.

## Why the `get_auth_config = lambda: None` monkey-patch

`agent.py` includes a monkey-patch that disables ADK's framework-level preemptive auth check. Without it, ADK triggers an OAuth redirect on every LLM invocation — even for prompts that would never call this tool — because the framework-level credential store uses a different key format than the tool-level store. This is a separate ADK bug (see the companion [`../01-preemptive_toolset_auth/`](../01-preemptive_toolset_auth/) reproduction). The workaround keeps this reproduction narrowly focused on the refresh-not-persisted bug.

## Companion bugs

- [`../02-scope_in_refresh/`](../02-scope_in_refresh/) — OAuth2 refresh fails for providers that reject `scope` parameter (e.g. Salesforce).
- [`../01-preemptive_toolset_auth/`](../01-preemptive_toolset_auth/) — preemptive toolset auth triggers OAuth on every agent invocation.
