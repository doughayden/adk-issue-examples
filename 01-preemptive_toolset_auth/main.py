"""WeatherAssistant Agent main script.

Reproduces the ADK preemptive-toolset-auth bug using the same agent-runtime
scaffolding as the upstream ``oauth2_client_credentials`` sample.

Differences from the upstream sample:

1. Starts the adapted ``oauth2_test_server`` as a subprocess (port 8080)
   and tears it down on exit.
2. Uses the ``authorization_code`` flow (not ``client_credentials``).
3. Sends a single non-tool prompt ("Hi! What can you do?"). Under the bug,
   even this prompt triggers an OAuth redirect — because
   ``_resolve_toolset_auth`` runs before the LLM decides whether any tool
   would be invoked.

Run from this directory:

    uv run python main.py                 # demonstrate the bug
    uv run python main.py --apply-fix     # demonstrate the fix resolves it

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
from pathlib import Path

import httpx
from dotenv import load_dotenv
from google.adk.agents.run_config import RunConfig
from google.adk.cli.utils import logs
from google.adk.runners import InMemoryRunner
from google.genai import types

import agent


def apply_proposed_fix() -> None:
    """Apply the proposed upstream fix: defer toolset auth to tool invocation.

    Monkey-patches ``weather_toolset.get_auth_config`` to return ``None``,
    which makes ADK's ``_resolve_toolset_auth`` skip the preemptive auth
    check for this toolset. Tool-level auth still happens on demand via
    ``ToolAuthHandler`` when the LLM actually invokes a tool.

    The upstream fix would be to remove the preemptive check from
    ``_resolve_toolset_auth`` entirely (or to unify the framework-level
    and tool-level credential key formats so the preemptive check can
    actually find credentials stored by successful tool invocations).
    """

    agent.weather_toolset.get_auth_config = lambda: None  # type: ignore[method-assign]


APP_NAME = "weather_assistant_app"
USER_ID = "weather_user"
SERVER_URL = "http://127.0.0.1:8080"

logs.setup_adk_logger(level=logging.ERROR)


def process_arguments() -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Weather Assistant Agent — reproduces ADK's preemptive-toolset-auth"
            " bug. Sending a non-tool prompt still triggers an OAuth redirect"
            " because _resolve_toolset_auth checks credentials before the LLM"
            " decides whether to call any tools."
        ),
        epilog=("Example usage:\n\tpython main.py\n\tpython main.py --apply-fix\n"),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default="Hi! What can you do?",
        help="Non-tool prompt (default: 'Hi! What can you do?').",
    )
    parser.add_argument(
        "--apply-fix",
        action="store_true",
        help=(
            "Monkey-patch the proposed upstream fix (disable preemptive "
            "toolset auth). With this flag, the agent responds normally. "
            "Without it, the agent triggers an OAuth redirect."
        ),
    )
    return parser.parse_args()


async def process_message(runner, session_id: str, message: str) -> str:
    """Process a single message with the weather assistant."""
    print(f"\n👤 User: {message}")
    print("🌤️  Weather Assistant: ", end="", flush=True)
    response = await call_agent_async(runner, USER_ID, session_id, message)
    print(f"{response}\n")
    return response


async def call_agent_async(runner, user_id: str, session_id: str, prompt: str) -> str:
    """Helper function to call agent asynchronously."""
    content = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
    final_response_text = ""

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=content,
        run_config=RunConfig(save_input_blobs_as_artifacts=False),
    ):
        # Surface all event types so auth-request events are visible.
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text and event.author != "user":
                    final_response_text += part.text
                if part.function_call:
                    print(
                        f"\n    [event: function_call {part.function_call.name}"
                        f" by {event.author}]"
                    )
                if part.function_response:
                    print(
                        f"\n    [event: function_response"
                        f" {part.function_response.name} by {event.author}]"
                    )

    return final_response_text


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

    if args.apply_fix:
        apply_proposed_fix()

    print("🌤️  WeatherAssistant Agent — preemptive-toolset-auth repro")
    print("=" * 60)
    print(f"Proposed fix applied: {args.apply_fix}")
    print(
        "Sending a prompt that shouldn't invoke any tool. Under the bug,"
        " ADK's _resolve_toolset_auth triggers an OAuth redirect anyway —"
        " before the LLM has even decided whether a tool would be called.\n"
    )

    # Start the OAuth2 test server
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
            await process_message(runner, session.id, args.prompt)
        except Exception as e:  # noqa: BLE001
            print(f"❌ Error: {e}", file=sys.stderr)
            return 1

        return 0
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
