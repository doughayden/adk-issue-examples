# Tool-level auth continues invocation past the EUC event

Demonstrates that ADK's tool-level auth path does not terminate the agent invocation when it yields `adk_request_credential` (the EUC), unlike the toolset-level path which sets `invocation_context.end_invocation = True` at the same logical point. The agent loop continues for at least one more LLM call after the EUC, producing a follow-up message that confuses the user (the agent has already shown the auth card; the second message tends to say something like "still need authorization") and creating a window where any post-auth resume task in a separate background context overlaps with the still-running first invocation.

## Files

Structure mirrors [`01-preemptive_toolset_auth/`](../01-preemptive_toolset_auth/):

- **`agent.py`** — `LlmAgent` with `OpenAPIToolset` pointed at the test server's `/api/weather` endpoint. Identical to 01, including the unmodified `get_auth_config`. The #5327 workaround is applied at runtime by `main.py` so we end up on the tool-level auth path.
- **`main.py`** — starts the test server, sends a tool-triggering prompt ("What's the weather in San Francisco?"), iterates the runner's event stream, and counts events by category. Reports whether the bug or the fix was observed based on event counts.
- **`oauth2_test_server.py`** — verbatim copy of 01's test server.

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

Wraps `BaseLlmFlow._postprocess_handle_function_calls_async` so that after each yielded event, if the event has `long_running_tool_ids` set (the auth event built by `functions.build_auth_request_event`), `invocation_context.end_invocation = True` is set on the way out. The wrapper preserves the existing event order and contents — only the termination flag is added. The next iteration of `run_async`'s outer loop sees `end_invocation` and breaks cleanly.

This mirrors the termination signal already used by `_resolve_toolset_auth` on the toolset-level path (`base_llm_flow.py` line 191 on `main`).

## Expected output

Both modes show the LLM yielding a `function_call` for `get_weather`, then the tool surfacing `adk_request_credential` because no credentials are available, then the function_response.

**Without fix:** the agent loop continues. A second LLM call fires and produces a text event after the EUC. `post_euc_text_events > 0`. Exit code 0 indicates the bug reproduced.

**With fix:** no events are yielded after the EUC. `post_euc_text_events == 0`. Exit code 0 indicates the fix takes effect.

## Why the #5327 workaround is always applied

The toolset-level path (`_resolve_toolset_auth`, base_llm_flow.py:115 on main) calls `tool_union.get_auth_config()` for every toolset before every LLM call. When that returns an `AuthConfig`, the framework checks for credentials before the LLM has decided whether any tool would be called, which is the bug demonstrated in [`01-preemptive_toolset_auth/`](../01-preemptive_toolset_auth/). The standard workaround is to set `toolset.get_auth_config = lambda: None`, which routes auth to the tool-level path that fires only when a tool is actually invoked. That workaround is exactly what surfaces the bug demonstrated here, so this example applies it at runtime to put the agent on the tool-level path.

## Companion bugs

- [`../01-preemptive_toolset_auth/`](../01-preemptive_toolset_auth/) — the bug whose workaround surfaces this one.
- [`../02-scope_in_refresh/`](../02-scope_in_refresh/) — OAuth2 refresh fails for providers that reject `scope` parameter.
- [`../03-refresh_not_persisted/`](../03-refresh_not_persisted/) — refreshed OAuth2 credentials are not persisted to the credential store.

## Related

A similar termination gap likely exists for `tool_confirmation_event` (HITL confirmation) at `_postprocess_handle_function_calls_async` lines 1129–1130 on main — same yield pattern, same `long_running_tool_ids` shape — but is not covered by this repro.
