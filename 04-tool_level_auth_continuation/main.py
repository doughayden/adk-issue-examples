"""WeatherAssistant Agent main script (tool-level auth continuation repro).

Reproduces an asymmetry in ADK's auth handling: when a tool requests
OAuth credentials at execution time (tool-level auth), the resulting
``adk_request_credential`` event does NOT terminate the invocation. The
agent loop continues to a second LLM call. The toolset-level path, by
contrast, sets ``invocation_context.end_invocation = True`` and
terminates cleanly at the same event.

Differences from ``01-preemptive_toolset_auth/main.py``:

1. Always applies the #5327 workaround
   (``weather_toolset.get_auth_config = lambda: None``) so the agent
   reaches the tool-level auth path. Any project that worked around
   #5327 has this in production.
2. Sends a prompt that triggers a tool call ("What's the weather in
   San Francisco?"), so the LLM invokes ``get_weather`` and the
   tool-level auth path actually fires.
3. ``--apply-fix`` patches
   ``BaseLlmFlow._postprocess_handle_function_calls_async`` to set
   ``end_invocation = True`` after yielding an event whose
   function_call is ``adk_request_credential`` (the auth event),
   demonstrating the proposed fix.

Run from this directory:

    uv run main.py                 # demonstrate the bug
    uv run main.py --apply-fix     # demonstrate the fix resolves it

Requires Gemini credentials (see ADK setup docs). A ``.env`` in this
directory with ``GOOGLE_API_KEY`` or Vertex AI configuration is loaded
automatically.
"""

import argparse
import asyncio
import logging
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
from dotenv import load_dotenv
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.run_config import RunConfig
from google.adk.cli.utils import logs
from google.adk.events import Event
from google.adk.flows.llm_flows.base_llm_flow import BaseLlmFlow
from google.adk.flows.llm_flows.functions import REQUEST_EUC_FUNCTION_CALL_NAME
from google.adk.models.llm_request import LlmRequest
from google.adk.runners import InMemoryRunner
from google.genai import types

import agent


def apply_5327_workaround() -> None:
    """Apply the existing #5327 workaround needed to reach tool-level auth.

    Without this, ADK's ``_resolve_toolset_auth`` triggers the
    toolset-level path, which already sets ``end_invocation = True``.
    Any project that worked around #5327 lands on the tool-level path,
    which is where the bug being demonstrated here lives.
    """

    agent.weather_toolset.get_auth_config = lambda: None  # type: ignore[method-assign]


def _is_auth_event(event: Event) -> bool:
    """Return True iff the event carries an adk_request_credential function call."""
    if not (event.content and event.content.parts):
        return False
    return any(
        part.function_call and part.function_call.name == REQUEST_EUC_FUNCTION_CALL_NAME
        for part in event.content.parts
    )


def apply_proposed_fix() -> None:
    """Apply the proposed upstream fix.

    Wraps ``BaseLlmFlow._postprocess_handle_function_calls_async`` so
    that after yielding an event whose function_call is
    ``adk_request_credential`` (the auth event built in
    ``functions.build_auth_request_event``), we set
    ``invocation_context.end_invocation = True``. This mirrors the
    termination signal already used by ``_resolve_toolset_auth`` on the
    toolset-level path (``base_llm_flow.py`` line 191 on main).

    The patched generator still yields all the same events in the same
    order — only the termination flag is added. The next iteration of
    ``run_async``'s outer loop sees ``end_invocation`` and breaks
    cleanly, with no second LLM call.

    Scope: this patch narrows to ``adk_request_credential`` events only.
    A similar termination gap exists for ``adk_request_confirmation``
    (HITL) at the same yield site, but is not addressed here so the
    patch matches the issue's stated scope.
    """

    original = BaseLlmFlow._postprocess_handle_function_calls_async

    async def patched(
        self: BaseLlmFlow,
        invocation_context: InvocationContext,
        function_call_event: Event,
        llm_request: LlmRequest,
    ) -> AsyncGenerator[Event]:
        async for event in original(
            self, invocation_context, function_call_event, llm_request
        ):
            yield event
            if _is_auth_event(event):
                invocation_context.end_invocation = True

    BaseLlmFlow._postprocess_handle_function_calls_async = patched  # type: ignore[method-assign]


APP_NAME = "weather_assistant_app"
USER_ID = "weather_user"
SERVER_URL = "http://127.0.0.1:8080"

logs.setup_adk_logger(level=logging.ERROR)


def process_arguments() -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Weather Assistant Agent — reproduces ADK's tool-level auth"
            " continuation bug. The agent yields adk_request_credential and"
            " then makes one more LLM call before terminating, instead of"
            " terminating at the EUC like the toolset-level path does."
        ),
        epilog=("Example usage:\n\tuv run main.py\n\tuv run main.py --apply-fix\n"),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default="What's the weather in San Francisco?",
        help=(
            "Prompt that triggers a tool call (default: 'What's the weather"
            " in San Francisco?')."
        ),
    )
    parser.add_argument(
        "--apply-fix",
        action="store_true",
        help=(
            "Monkey-patch the proposed upstream fix (set end_invocation = True"
            " after yielding the auth_event in the tool-level auth path)."
        ),
    )
    return parser.parse_args()


async def run_and_log(runner, session_id: str, message: str) -> dict[str, int]:
    """Run the agent and log every event with category counters.

    Returns counts of events observed by category, so the bug shows up
    as ``post_euc_text_events > 0``.
    """

    print(f"\n👤 User: {message}")
    print("🌤️  Weather Assistant event stream:\n")

    content = types.Content(role="user", parts=[types.Part.from_text(text=message)])
    counts = {
        "function_calls": 0,
        "auth_events": 0,
        "function_responses": 0,
        "text_events": 0,
        "post_euc_text_events": 0,
    }
    seen_euc = False

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=content,
        run_config=RunConfig(save_input_blobs_as_artifacts=False),
    ):
        if not (event.content and event.content.parts):
            continue
        for part in event.content.parts:
            if part.function_call:
                name = part.function_call.name
                if name == "adk_request_credential":
                    counts["auth_events"] += 1
                    seen_euc = True
                    print(f"    [auth_event] adk_request_credential by {event.author}")
                else:
                    counts["function_calls"] += 1
                    print(f"    [function_call] {name} by {event.author}")
            elif part.function_response:
                counts["function_responses"] += 1
                print(
                    f"    [function_response] {part.function_response.name}"
                    f" by {event.author}"
                )
            elif part.text:
                counts["text_events"] += 1
                preview = part.text.strip().replace("\n", " ")
                if len(preview) > 80:
                    preview = preview[:77] + "..."
                tag = "post_euc_text" if seen_euc and event.author != "user" else "text"
                if seen_euc and event.author != "user":
                    counts["post_euc_text_events"] += 1
                print(f"    [{tag}] {event.author}: {preview!r}")

    return counts


def wait_for_server(url: str, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=0.5)
            return True
        except httpx.HTTPError:
            time.sleep(0.1)
    return False


async def main() -> int:
    """Main function."""
    load_dotenv()
    args = process_arguments()

    apply_5327_workaround()
    if args.apply_fix:
        apply_proposed_fix()

    print("🌤️  WeatherAssistant Agent — tool-level auth continuation repro")
    print("=" * 60)
    print("#5327 workaround applied:  True (always — needed to reach tool-level path)")
    print(f"Proposed fix applied:      {args.apply_fix}")
    print(
        "\nSending a prompt that triggers a tool call. The tool needs OAuth"
        " credentials it doesn't have, so ADK yields adk_request_credential."
        " Without the proposed fix, the agent loop continues to a second LLM"
        " call after the EUC. With the fix, it terminates cleanly.\n"
    )

    server_script = Path(__file__).parent / "oauth2_test_server.py"
    server_proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(server_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_for_server(SERVER_URL):
            print("❌ OAuth2 test server failed to start", file=sys.stderr)
            return 1

        runner = InMemoryRunner(agent=agent.root_agent, app_name=APP_NAME)
        session = await runner.session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID
        )

        try:
            counts = await run_and_log(runner, session.id, args.prompt)
        except Exception as e:  # noqa: BLE001
            print(f"❌ Error: {e}", file=sys.stderr)
            return 1

        print("\nEvent counts:")
        for k, v in counts.items():
            print(f"    {k}: {v}")

        print()
        if args.apply_fix:
            if counts["post_euc_text_events"] == 0:
                print("✅ Fix verified: no LLM events after the EUC.")
                return 0
            print(
                "❌ Fix did not take effect:"
                f" {counts['post_euc_text_events']} text event(s) after the EUC."
            )
            return 1
        if counts["post_euc_text_events"] > 0:
            print(
                "✅ Bug reproduced:"
                f" {counts['post_euc_text_events']} text event(s) after the EUC"
                " (agent loop continued past adk_request_credential)."
            )
            return 0
        print(
            "⚠️  Bug not reproduced: agent terminated at the EUC. The"
            " framework may have changed behavior in this ADK version."
        )
        return 1
    finally:
        server_proc.terminate()
        server_proc.wait(timeout=5)


if __name__ == "__main__":
    start_time = time.time()
    print(
        "⏰ Started at"
        f" {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time))}"
    )
    print("-" * 50)

    try:
        exit_code = asyncio.run(main())
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
