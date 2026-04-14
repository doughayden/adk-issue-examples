# Preemptive toolset auth triggers OAuth on every invocation

Demonstrates that `_resolve_toolset_auth` in `base_llm_flow.py` runs before every agent invocation and calls `get_auth_config()` on each toolset. If the toolset returns an `AuthConfig`, the framework checks for credentials **before the LLM has decided whether any tool call is needed**. When the framework-level credential lookup fails — which it does for tool-level-authenticated credentials because the two paths use different key formats — this triggers a full OAuth redirect flow on every message, including simple greetings that will never invoke any tool.

## Files

Structure mirrors [`contributing/samples/oauth2_client_credentials`](https://github.com/google/adk-python/tree/main/contributing/samples/oauth2_client_credentials):

- **`agent.py`** — `LlmAgent` with `OpenAPIToolset` pointed at the test server's `/api/weather` endpoint. Uses `authorization_code` flow. **Does not** include the `get_auth_config = lambda: None` monkey-patch — that patch _is_ the workaround for this bug.
- **`main.py`** — starts the test server, creates a session, sends a single non-tool prompt. Under the bug, the agent triggers an OAuth redirect before the LLM runs. With the fix, the LLM responds normally.
- **`oauth2_test_server.py`** — adapted from the ADK sample's test server.

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

The `--apply-fix` flag monkey-patches `weather_toolset.get_auth_config` to return `None`, which disables ADK's framework-level preemptive auth check for this toolset. Tool-level auth still fires on demand via `ToolAuthHandler` when the LLM actually invokes a tool. The upstream fix would be to remove the preemptive check from `_resolve_toolset_auth` entirely (or to unify the framework-level and tool-level credential key formats).

## Expected output

**Without fix:** sending a non-tool prompt ("Hi! What can you do?") emits an `adk_request_credential` function call — an OAuth redirect — before the LLM has even run. The agent can't respond with text.

**With fix:** the agent responds normally with text, no OAuth redirect.

## Companion bugs

- [`../03-refresh_not_persisted/`](../03-refresh_not_persisted/) — refreshed OAuth2 credentials are not persisted to the credential store.
- [`../02-scope_in_refresh/`](../02-scope_in_refresh/) — OAuth2 refresh fails for providers that reject `scope` parameter (e.g. Salesforce).
