# adk-issue-examples

Runnable reproductions for issues filed in [google/adk-python](https://github.com/google/adk-python).

Each subdirectory is a self-contained example for a specific bug — structured around an `LlmAgent` + `InMemoryRunner` flow, with a local OAuth2 test server. Entry points include a `--apply-fix` CLI flag that monkey-patches the proposed upstream fix so the same script demonstrates both the bug and its resolution.

## Examples

| # | Directory | Bug | Issue |
|---|-----------|-----|-------|
| 01 | [`01-preemptive_toolset_auth/`](01-preemptive_toolset_auth/) | Preemptive toolset auth triggers OAuth redirect on every agent invocation | TBD |
| 02 | [`02-scope_in_refresh/`](02-scope_in_refresh/) | OAuth2 token refresh fails for providers that reject `scope` parameter (e.g. Salesforce) | TBD |
| 03 | [`03-refresh_not_persisted/`](03-refresh_not_persisted/) | `ToolAuthHandler._get_existing_credential` refreshes OAuth2 credentials in memory but doesn't persist them | TBD |

## Running an example

Each example needs model credentials (Vertex AI shown). From an example directory:

```bash
# One-time: authenticate
gcloud auth application-default login

# Create .env with your project info
cat > .env <<EOF
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=<your-project>
GOOGLE_CLOUD_LOCATION=us-central1
EOF

# Install dependencies and run the bug reproduction
uv sync
uv run python main.py

# Run with the proposed upstream fix applied
uv run python main.py --apply-fix
```

Each example's `README.md` documents the expected output for both modes.

## Test server

`oauth2_test_server.py` (one copy per example) is adapted from ADK's [`oauth2_client_credentials` sample server](https://github.com/google/adk-python/blob/main/contributing/samples/oauth2_client_credentials/oauth2_test_server.py) with two minimal additions:

- **`refresh_token` grant** with refresh_token rotation. Rotation is standard provider security practice and surfaces bugs that depend on state across multiple refreshes.
- **`STRICT_SCOPE_REJECTION=1` env var** — when set, the refresh handler returns `400 invalid_request: scope parameter not supported` for any refresh request that includes a `scope` parameter. Mimics Salesforce behavior for the example in `02-scope_in_refresh/`.

## License

MIT — see [LICENSE](LICENSE).
