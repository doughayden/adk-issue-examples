# Eval inference drops the App's plugins

Demonstrates that ADK's eval inference builds its `Runner` from the bare `root_agent` and never applies the wrapping `App`'s plugins. `EvaluationGenerator._generate_inferences_from_root_agent` constructs `Runner(app_name=..., agent=root_agent, plugins=[<internal eval plugins>])`, so `app.plugins` (the global instruction, logging, telemetry, and any guardrail plugins a project adds) are silently dropped for the duration of an eval run.

The consequence: an eval scores a different agent than `adk web` chat and the deployed server run, both of which execute the full `App`. It also feeds empty context to the LLM-judge metrics that read `app_details` (developer instructions plus tool declarations), because those are shaped by the plugins the eval never applied.

## Files

- **`example_agent/agent.py`** — an `LlmAgent` wrapped in an `App` whose `plugins` include a `SentinelPlugin`. The plugin's `before_run_callback` appends to a module-level `PLUGIN_INVOCATIONS` list every time it runs inside a `Runner`, so "did the App's plugins apply during eval" becomes directly observable.
- **`example_agent/__init__.py`** — exposes the `agent` submodule so `AgentEvaluator` resolves `example_agent.agent.root_agent` from the module name `example_agent`.
- **`example_agent.evalset.json`** — one trivial turn. The response content and metric scores are incidental; the plugin drop is the point.
- **`test_config.json`** — a lenient `response_match_score` criterion (auto-discovered from the eval set's directory).
- **`main.py`** — probes the installed ADK for App-aware eval inference, runs `AgentEvaluator.evaluate`, and inspects `PLUGIN_INVOCATIONS` to report whether the App's plugins were applied.

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
# Released ADK: the App's plugins are dropped
uv run main.py

# Branch carrying the fix: the App's plugins apply
uv run --isolated --no-project \
    --with "google-adk[eval] @ git+https://github.com/doughayden/adk-python@fix/eval-app-aware-inference" \
    --with python-dotenv \
    main.py
```

Nothing is patched or stubbed in either run. The pinned dependency in `pyproject.toml` selects the released ADK; the second command swaps in the branch and runs the same script unchanged, so what it exercises is the real caller chain (`AgentEvaluator` → `LocalEvalService` → `EvaluationGenerator`).

`main.py` probes the installed ADK for an `app` parameter on the inference leaf and reports the result, then checks the observed plugin behavior against it. A build without the parameter is expected to drop the plugins; a build with it is expected to apply them. Either mismatch exits non-zero.

## What the fix does

The eval Runner is built from `app.model_copy(update={"plugins": list(app.plugins) + internal_eval_plugins, "root_agent": root_agent})` instead of the bare agent, so it carries the App's plugins (plus the two internal eval plugins) along with the App's `context_cache_config` and `resumability_config`. The `App` reaches that leaf by being threaded through the eval callers: `AgentEvaluator._get_eval_results_by_eval_id`, the `dev_server` eval handler, and the `adk eval` CLI.

## Expected output

**Released ADK:** `AgentEvaluator.evaluate` runs the bare `root_agent`, so the `SentinelPlugin` never runs. `PLUGIN_INVOCATIONS` is empty. Exit code 0 indicates the bug reproduced.

**Branch with the fix:** the eval Runner is built from the `App`, so the `SentinelPlugin` runs during inference. `PLUGIN_INVOCATIONS` contains `['sentinel']`. Exit code 0 indicates the fix takes effect.

## Scope

This reproduction exercises the non-live inference leaf (`_generate_inferences_from_root_agent`), which is what `AgentEvaluator.evaluate` reaches. The live leaf (`_generate_inferences_from_root_agent_live`, used when `use_live=True`) had the same bare-agent construction and the branch gives it the same treatment through a shared Runner-construction helper; exercising it needs a live-API session, so it is covered by unit tests upstream rather than here. `main.py` reports whether the installed build carries the App through the live leaf as well.
