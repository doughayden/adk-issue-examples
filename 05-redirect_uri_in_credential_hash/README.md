# Credential lookup misses across redirect_uri changes

Demonstrates that `ToolContextCredentialStore.get_credential_key` hashes `redirect_uri` along with the rest of the OAuth2 credential. Two credentials that share the same OAuth identity (client_id, client_secret, scopes, access_token, refresh_token) but differ only in `redirect_uri` produce different hash keys, so a credential minted under one deployment URL is unfindable when the deployment moves to another.

`redirect_uri` is deployment configuration, not credential identity. Including it in the hash defeats the credential store's purpose across multi-environment workflows (local dev, staging, production) and during routine configuration changes like a relay URL rotation.

## Files

Structure mirrors [`03-refresh_not_persisted/`](../03-refresh_not_persisted/):

- **`agent.py`** — `LlmAgent` with `OpenAPIToolset` pointed at the test server's `/api/weather` endpoint. Built with `CURRENT_REDIRECT_URI` (the post-deployment-change URL). The #5327 workaround (`weather_toolset.get_auth_config = lambda: None`) is applied at module-load so the agent reaches the tool-level credential store, where the bug lives.
- **`main.py`** — starts the test server, completes a real authorization_code flow to mint a valid (non-expired) access_token, seeds it into session state under the hash key computed from `STORED_REDIRECT_URI` (the original deployment URL), then runs the agent and watches the event stream. Prints the two hash keys side-by-side before the agent runs so the bug is visible at the hash-key level too.
- **`oauth2_test_server.py`** — verbatim copy of 03's / 04's test server.

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
uv run main.py

# Run with the proposed upstream fix applied
uv run main.py --apply-fix
```

## What `--apply-fix` does

Wraps both `ToolContextCredentialStore.get_credential_key` and `ToolContextCredentialStore._get_legacy_credential_key` so that `redirect_uri` is stripped from the `auth_credential` before delegating to ADK's original hashing. This is the proposed upstream patch: add `auth_credential.oauth2.redirect_uri = None` to the existing strip block in both methods.

## Expected output

**Without fix:** the two hash keys differ. The agent emits a `function_call get_weather` followed by an `adk_request_credential` event — the seeded credential was present in state but unfindable under the agent's runtime hash key. `auth_events > 0`. Exit code 0 indicates the bug reproduced.

**With fix:** the two hash keys are identical. The agent emits `function_call get_weather`, then `function_response get_weather` carrying real weather data, and a final assistant text. No `adk_request_credential` event. `auth_events == 0` and `function_responses > 0`. Exit code 0 indicates the fix takes effect.

## Why the `get_auth_config = lambda: None` monkey-patch

`agent.py` includes a monkey-patch that disables ADK's framework-level preemptive auth check. Without it, ADK triggers an OAuth redirect on every LLM invocation — even for prompts that would never call this tool — because the framework-level credential store uses a different key format than the tool-level store. This is a separate ADK bug (see [`../01-preemptive_toolset_auth/`](../01-preemptive_toolset_auth/)). The workaround keeps this reproduction narrowly focused on the redirect_uri-in-hash bug.

## A note on the framework-level path

`AuthConfig.get_credential_key()` in `src/google/adk/auth/auth_tool.py` has the same gap in its strip block — it omits `redirect_uri` for the same reason. The framework-level path is disabled in this repro by the `get_auth_config = lambda: None` workaround, so the fix here patches only the tool-level methods. A complete upstream fix would patch both call sites.

## Companion bugs

- [`../01-preemptive_toolset_auth/`](../01-preemptive_toolset_auth/) — preemptive toolset auth triggers OAuth on every agent invocation.
- [`../02-scope_in_refresh/`](../02-scope_in_refresh/) — OAuth2 refresh fails for providers that reject `scope` parameter.
- [`../03-refresh_not_persisted/`](../03-refresh_not_persisted/) — refreshed OAuth2 credentials are not persisted.
- [`../04-tool_level_auth_continuation/`](../04-tool_level_auth_continuation/) — tool-level auth continues past the EUC event.
