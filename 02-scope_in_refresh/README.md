# OAuth2 token refresh fails for providers that reject `scope`

Demonstrates that `OAuth2CredentialRefresher.refresh()` transitively includes the `scope` parameter in refresh token requests because `create_oauth2_session()` passes scopes to authlib's `OAuth2Session` constructor. Salesforce rejects refresh requests that include `scope`, causing automatic token refreshes to fail silently for Salesforce-backed toolsets.

Per RFC 6749 §6, `scope` is OPTIONAL in refresh requests — when omitted, providers treat it as equal to the originally-granted scope. Since sending `scope` on refresh provides no functional benefit (it can only narrow, not broaden the granted scope) and some providers actively reject it, ADK should default to omitting it for maximum compatibility.

## Files

Structure mirrors [`contributing/samples/oauth2_client_credentials`](https://github.com/google/adk-python/tree/main/contributing/samples/oauth2_client_credentials):

- **`agent.py`** — `LlmAgent` with `OpenAPIToolset` pointed at the test server's `/api/weather` endpoint. Uses `authorization_code` flow with defined scopes; the bug fires when ADK refreshes those scopes.
- **`main.py`** — starts the test server with `STRICT_SCOPE_REJECTION=1`, seeds an expired credential into session state, runs a single weather query to trigger the refresh.
- **`oauth2_test_server.py`** — adapted from the ADK sample's test server; rejects refresh requests with a `scope` parameter when the env var is set.

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

The `--apply-fix` flag patches the refresher module's `create_oauth2_session` to null out `session.scope` after construction so authlib doesn't include `scope` in the refresh request body. The upstream fix would be to simply not pass `scope` to the `OAuth2Session` constructor in `oauth2_credential_util.create_oauth2_session`.

## Expected output

**Without fix:** the query fails. ADK refreshes the expired seed credential; the server rejects the request because it includes a `scope` parameter; ADK falls through to `_request_credential`. The agent says it needs authorization.

**With fix applied:** the query succeeds — the refresh completes cleanly without `scope` in the request body, the tool returns weather data.

> [!NOTE]
> This example runs a single query to stay narrowly focused on the
> scope-in-refresh bug. A second query in the same session would expose
> the companion
> [`../03-refresh_not_persisted/`](../03-refresh_not_persisted/) bug —
> even after our `--apply-fix` resolves the scope issue, the refreshed
> credential isn't persisted to the store, so the next tool call
> re-attempts refresh with the rotated refresh_token and fails.

## Why the `get_auth_config = lambda: None` workaround

`agent.py` includes a monkey-patch that disables ADK's framework-level preemptive auth check. Without it, ADK triggers an OAuth redirect on every LLM invocation — even for prompts that wouldn't call this tool — because the framework-level credential store uses a different key format than the tool-level store. This is a separate ADK bug (see the companion [`../01-preemptive_toolset_auth/`](../01-preemptive_toolset_auth/) reproduction). The workaround keeps this reproduction narrowly focused on the scope-in-refresh bug.

## Companion bugs

- [`../03-refresh_not_persisted/`](../03-refresh_not_persisted/) — refreshed OAuth2 credentials are not persisted to the credential store.
- [`../01-preemptive_toolset_auth/`](../01-preemptive_toolset_auth/) — preemptive toolset auth triggers OAuth on every agent invocation.
