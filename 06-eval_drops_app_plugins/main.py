"""Eval-drops-App-plugins reproduction (adk-python#5503).

ADK's eval inference builds its ``Runner`` from the bare ``root_agent`` and
never applies the wrapping ``App``'s plugins. ``EvaluationGenerator.
_generate_inferences_from_root_agent`` constructs
``Runner(app_name=..., agent=root_agent, plugins=[<internal eval plugins>])``,
so ``app.plugins`` (the global instruction, logging, telemetry, and any
guardrail plugins a project adds) are silently dropped. The eval therefore
scores a different agent than ``adk web`` chat and the deployed server run,
and LLM-judge metrics that read ``app_details`` (developer instructions plus
tool declarations) score against context those plugins were supposed to shape.

This example makes the drop directly observable. ``example_agent/agent.py`` wraps
the root agent in an ``App`` with a ``SentinelPlugin`` whose
``before_run_callback`` appends to ``PLUGIN_INVOCATIONS`` every time it runs
inside a ``Runner``. Running ``AgentEvaluator.evaluate`` and then inspecting
that list shows whether the App's plugins were applied during eval inference.

``--apply-fix`` monkey-patches ``_generate_inferences_from_root_agent`` to
build the ``Runner`` from ``app.model_copy(update={"plugins": list(app.plugins)
+ internal_eval_plugins, "root_agent": root_agent})`` instead of the bare
agent. That is the proposed fix: the eval Runner carries the App's plugins
(plus the two internal eval plugins) and the App's ``context_cache_config`` /
``resumability_config``. Upstream, the ``app`` reaches this leaf by being
threaded through the eval callers; this example resolves it directly from the
agent package to keep the reproduction self-contained.

Run from this directory:

    uv run main.py                 # reproduce the bug (App plugins dropped)
    uv run main.py --apply-fix     # apply the fix (App plugins applied)

Requires Vertex AI credentials (see ADK setup docs). A ``.env`` in this
directory with the Vertex configuration is loaded automatically.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.adk.evaluation import evaluation_generator as _eg
from google.adk.runners import Runner

from example_agent import agent
from example_agent.agent import app as _example_app

AGENT_MODULE = "example_agent"
EVAL_SET = Path(__file__).parent / "example_agent.evalset.json"


def apply_proposed_fix() -> None:
    """Replace the eval-inference leaf with an App-aware version.

    Mirrors the upstream direction (build the eval Runner from the ``App`` so
    ``app.plugins`` apply), resolving the ``App`` from this example's agent
    package. Body copied from ADK's
    ``EvaluationGenerator._generate_inferences_from_root_agent`` with the single
    change being the ``Runner`` construction.
    """

    async def _app_aware_leaf(
        root_agent: Any,
        user_simulator: Any,
        reset_func: Any = None,
        initial_session: Any = None,
        session_id: str | None = None,
        session_service: Any = None,
        artifact_service: Any = None,
        memory_service: Any = None,
    ) -> list[Any]:
        if not session_service:
            session_service = _eg.InMemorySessionService()
        if not memory_service:
            memory_service = _eg.InMemoryMemoryService()

        app_name = (
            initial_session.app_name if initial_session else "EvaluationGenerator"
        )
        user_id = initial_session.user_id if initial_session else "test_user_id"
        session_id = session_id if session_id else str(_eg.uuid.uuid4())

        _ = await session_service.create_session(
            app_name=app_name,
            user_id=user_id,
            state=initial_session.state if initial_session else {},
            session_id=session_id,
        )

        if not artifact_service:
            artifact_service = _eg.InMemoryArtifactService()

        if callable(reset_func):
            reset_func()

        request_intercepter_plugin = _eg._RequestIntercepterPlugin(
            name="request_intercepter_plugin"
        )
        ensure_retry_options_plugin = _eg.EnsureRetryOptionsPlugin(
            name="ensure_retry_options"
        )
        internal_eval_plugins = [
            request_intercepter_plugin,
            ensure_retry_options_plugin,
        ]

        # --- The fix: build the Runner from the App, not the bare agent. ---
        runner_app = _example_app.model_copy(
            update={
                "plugins": list(_example_app.plugins) + internal_eval_plugins,
                "root_agent": root_agent,
            }
        )
        generate_invocation = (
            _eg.EvaluationGenerator._generate_inferences_for_single_user_invocation
        )
        async with Runner(
            app=runner_app,
            app_name=app_name,
            artifact_service=artifact_service,
            session_service=session_service,
            memory_service=memory_service,
        ) as runner:
            events = []
            while True:
                next_user_message = await user_simulator.get_next_user_message(
                    _eg.copy.deepcopy(events)
                )
                if next_user_message.status == _eg.UserSimulatorStatus.SUCCESS:
                    async for event in generate_invocation(
                        runner, user_id, session_id, next_user_message.user_message
                    ):
                        events.append(event)
                else:
                    break

            app_details_by_invocation_id = (
                _eg.EvaluationGenerator._get_app_details_by_invocation_id(
                    events, request_intercepter_plugin
                )
            )
            return _eg.EvaluationGenerator.convert_events_to_eval_invocations(
                events, app_details_by_invocation_id
            )

    _eg.EvaluationGenerator._generate_inferences_from_root_agent = staticmethod(
        _app_aware_leaf
    )


async def run_eval() -> None:
    """Run one eval pass through ADK's public ``AgentEvaluator``.

    Metric thresholds are incidental to this reproduction — the App-plugin drop
    is what we demonstrate — so a sub-threshold score is caught and noted.
    """
    from google.adk.evaluation.agent_evaluator import AgentEvaluator

    try:
        await AgentEvaluator.evaluate(
            agent_module=AGENT_MODULE,
            eval_dataset_file_path_or_dir=str(EVAL_SET),
            num_runs=1,
        )
    except AssertionError:
        print("   (eval metric scores are incidental to this reproduction)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply-fix",
        action="store_true",
        help="apply the proposed App-aware eval-inference fix",
    )
    args = parser.parse_args()
    load_dotenv()

    if args.apply_fix:
        apply_proposed_fix()
        print("🔧 Applied the proposed fix: App-aware eval inference.\n")
    else:
        print("🐞 Running unpatched ADK (reproducing the bug).\n")

    asyncio.run(run_eval())

    fired = agent.PLUGIN_INVOCATIONS
    print(f"\nApp-level plugin invocations during eval inference: {fired}")

    if args.apply_fix:
        if fired:
            print(
                "✅ Fix verified: the App's plugins ran during eval inference,"
                " so the eval scores the same agent production runs."
            )
            return 0
        print("❌ Fix did not take effect: the App's plugins still did not run.")
        return 1

    if not fired:
        print(
            "✅ Bug reproduced: the App's plugins were dropped — none ran during"
            " eval inference, so the eval scored the bare root_agent."
        )
        return 0
    print(
        "⚠️  Bug not reproduced: the App's plugins ran unexpectedly (ADK"
        " behavior may have changed in this version)."
    )
    return 1


if __name__ == "__main__":
    start_time = time.time()
    print(
        "⏰ Started at"
        f" {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time))}"
    )
    print("-" * 50)

    try:
        exit_code = main()
    except KeyboardInterrupt:
        print("\n⏹️  Interrupted by user")
        exit_code = 1

    end_time = time.time()
    print("-" * 50)
    print(
        f"⏰ Finished at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end_time))}"
    )
    print(f"⌛ Total execution time: {end_time - start_time:.2f} seconds")

    sys.exit(exit_code)
