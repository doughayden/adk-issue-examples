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

This example makes the drop directly observable. ``example_agent/agent.py``
wraps the root agent in an ``App`` with a ``SentinelPlugin`` whose
``before_run_callback`` appends to ``PLUGIN_INVOCATIONS`` every time it runs
inside a ``Runner``. Running ``AgentEvaluator.evaluate`` and then inspecting
that list shows whether the App's plugins were applied during eval inference.

Nothing here is patched or stubbed. The script probes the installed ADK for
App-aware eval inference, runs one real eval through the public
``AgentEvaluator.evaluate`` entry point, and checks that the observed plugin
behavior matches what that build supports. Run it against the released ADK to
see the drop, and against the branch carrying the fix to see the App's plugins
survive the full caller chain (``AgentEvaluator`` -> ``LocalEvalService`` ->
``EvaluationGenerator``).

Run from this directory:

    # Released ADK: the App's plugins are dropped
    uv run main.py

To run the same script against the branch carrying the fix, use the
``uv run --isolated --no-project --with ...`` command in ``README.md``, which
swaps in the branch build without changing anything here.

Requires Vertex AI credentials (see ADK setup docs). A ``.env`` in this
directory with the Vertex configuration is loaded automatically.
"""

import asyncio
import inspect
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google.adk.evaluation.evaluation_generator import EvaluationGenerator
from google.adk.version import __version__ as adk_version

from example_agent import agent

AGENT_MODULE = "example_agent"
EVAL_SET = Path(__file__).parent / "example_agent.evalset.json"


def supports_app_aware_eval() -> bool:
    """Reports whether the installed ADK threads an ``App`` into eval inference.

    The fix adds an ``app`` parameter to the inference leaf that every non-live
    eval surface funnels into, so its presence identifies a build able to carry
    the App's plugins through an eval run.
    """
    leaf = EvaluationGenerator._generate_inferences_from_root_agent
    return "app" in inspect.signature(leaf).parameters


def supports_app_aware_live_eval() -> bool:
    """Reports the same for the live inference leaf, where that leaf exists."""
    leaf = getattr(
        EvaluationGenerator, "_generate_inferences_from_root_agent_live", None
    )
    return leaf is not None and "app" in inspect.signature(leaf).parameters


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
    load_dotenv()

    app_aware = supports_app_aware_eval()
    print(f"google-adk {adk_version}")
    print(f"App-aware eval inference: {app_aware}")
    print(f"App-aware live eval inference: {supports_app_aware_live_eval()}")

    if app_aware:
        print("\n🔧 Running an ADK build that carries the App through eval.\n")
    else:
        print("\n🐞 Running an ADK build without the fix.\n")

    asyncio.run(run_eval())

    fired = agent.PLUGIN_INVOCATIONS
    print(f"\nApp-level plugin invocations during eval inference: {fired}")

    if app_aware:
        if fired:
            print(
                "✅ Fix verified end to end: the App's plugins ran during eval"
                " inference, so the eval scores the same agent production runs."
            )
            return 0
        print(
            "❌ This build carries the App-aware parameter, but the App's"
            " plugins still did not run."
        )
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
