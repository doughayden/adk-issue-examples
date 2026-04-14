"""WeatherAssistant Agent main script.

Reproduces the ADK OAuth2 scope-in-refresh bug using the same agent-runtime
scaffolding as the upstream ``oauth2_client_credentials`` sample.

Differences from the upstream sample:

1. Starts the adapted ``oauth2_test_server`` as a subprocess (port 8080)
   with ``STRICT_SCOPE_REJECTION=1`` so it rejects refresh requests that
   include a ``scope`` parameter (mimics Salesforce behavior).
2. Uses the ``authorization_code`` flow (not ``client_credentials``) so
   that refresh_tokens are issued — the bug lives in the refresh path.
3. Sends a single weather query. Under the bug, the refresh fails silently;
   ADK falls through to ``_request_credential`` and the user is prompted
   to re-authorize. (A second query would also expose the companion
   refresh-not-persisted bug, so this example stays narrowly focused.)

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
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from google.adk.agents.run_config import RunConfig
from google.adk.auth.auth_credential import (
    AuthCredential,
    AuthCredentialTypes,
    OAuth2Auth,
)
from google.adk.auth.auth_tool import AuthConfig
from google.adk.auth.refresher import oauth2_credential_refresher
from google.adk.cli.utils import logs
from google.adk.runners import InMemoryRunner
from google.adk.tools.openapi_tool.openapi_spec_parser.tool_auth_handler import (
    ToolContextCredentialStore,
)
from google.genai import types

import agent


def apply_proposed_fix() -> None:
    """Apply the proposed upstream fix: don't send `scope` in refresh requests.

    Patches the refresher module's ``create_oauth2_session`` to null out
    ``session.scope`` after construction so authlib doesn't include it in
    the refresh request body. The upstream fix would be to simply not pass
    ``scope`` to the ``OAuth2Session`` constructor in
    ``oauth2_credential_util.create_oauth2_session``.

    Note: the refresher imports ``create_oauth2_session`` via
    ``from ... import ...``, so the patch must target the refresher
    module's local binding — not the source module.
    """

    original = oauth2_credential_refresher.create_oauth2_session

    def _no_scope(auth_scheme, auth_credential):  # type: ignore[no-untyped-def]
        session, endpoint = original(auth_scheme, auth_credential)
        if session is not None:
            session.scope = None
        return session, endpoint

    oauth2_credential_refresher.create_oauth2_session = _no_scope


APP_NAME = "weather_assistant_app"
USER_ID = "weather_user"
SERVER_URL = "http://127.0.0.1:8080"

logs.setup_adk_logger(level=logging.ERROR)


def process_arguments() -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Weather Assistant Agent — reproduces ADK's OAuth2 scope-in-refresh"
            " bug. With a provider that rejects `scope` in refresh requests,"
            " ADK's automatic refresh fails silently and the user is prompted"
            " to re-authorize."
        ),
        epilog=("Example usage:\n\tpython main.py\n\tpython main.py --apply-fix\n"),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default="What's the weather in Tokyo?",
        help="Weather prompt (default: Tokyo).",
    )
    parser.add_argument(
        "--apply-fix",
        action="store_true",
        help=(
            "Monkey-patch the proposed upstream fix (strip scope from the "
            "refresh request). With this flag, the refresh succeeds. Without "
            "it, the refresh fails and the agent prompts for re-auth."
        ),
    )
    return parser.parse_args()


async def process_message(runner, session_id: str, message: str) -> str:
    """Process a single message with the weather assistant."""
    print(f"\n👤 User: {message}")
    print("🌤️  Weather Assistant: ", flush=True)
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
                        f"    [event: function_call {part.function_call.name}"
                        f" by {event.author}]"
                    )
                if part.function_response:
                    print(
                        f"    [event: function_response"
                        f" {part.function_response.name} by {event.author}]"
                    )

    return final_response_text


def seed_credential_via_auth_code_flow() -> AuthCredential:
    """Complete the authorization_code flow against the local server."""
    auth_resp = httpx.get(
        agent.AUTH_URL,
        params={
            "response_type": "code",
            "client_id": agent.CLIENT_ID,
            "redirect_uri": agent.REDIRECT_URI,
            "scope": "read",
        },
        follow_redirects=False,
    )
    code = auth_resp.headers["location"].split("code=")[1].split("&")[0]

    token_resp = httpx.post(
        agent.TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": agent.CLIENT_ID,
            "client_secret": agent.CLIENT_SECRET,
            "redirect_uri": agent.REDIRECT_URI,
        },
    )
    token_resp.raise_for_status()
    tokens = token_resp.json()

    return AuthCredential(
        auth_type=AuthCredentialTypes.OAUTH2,
        oauth2=OAuth2Auth(
            client_id=agent.CLIENT_ID,
            client_secret=agent.CLIENT_SECRET,
            redirect_uri=agent.REDIRECT_URI,
            token_endpoint_auth_method="client_secret_post",  # noqa: S106
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            expires_at=1,  # past — forces ADK to refresh on first tool call
        ),
    )


def build_seeded_state(seed: AuthCredential) -> dict:
    """Pre-populate session state under both framework-level and tool-level keys.

    ADK has two credential stores with different key formats:

    * Framework-level (``AuthConfig.credential_key``): ``adk_{scheme}_{cred}``
      — checked preemptively by ``_resolve_toolset_auth`` on every LLM call.
    * Tool-level (``ToolContextCredentialStore.get_credential_key``):
      ``{scheme}_{cred}_existing_exchanged_credential`` — used by
      ``ToolAuthHandler`` during the actual tool call.

    Seeding under both avoids unrelated re-auth prompts from the preemptive
    check (see companion bug report on preemptive toolset auth) and lets us
    isolate the scope-in-refresh bug.
    """
    auth_scheme = agent.build_auth_scheme()
    raw_credential = agent.build_auth_credential()
    seed_dict = seed.model_dump(exclude_none=True)

    # Tool-level key
    tool_store = ToolContextCredentialStore(tool_context=None)
    tool_key = tool_store.get_credential_key(auth_scheme, raw_credential)

    # Framework-level key
    framework_key = AuthConfig(
        auth_scheme=auth_scheme, raw_auth_credential=raw_credential
    ).credential_key

    state: dict = {tool_key: seed_dict}
    if framework_key:
        state[framework_key] = seed_dict
    return state


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

    print("🌤️  WeatherAssistant Agent — scope-in-refresh repro")
    print("=" * 60)
    print(f"Proposed fix applied: {args.apply_fix}")
    print(
        "The local OAuth2 server rejects refresh requests that include a"
        " `scope` parameter (mimics Salesforce). ADK's create_oauth2_session"
        " unconditionally passes scopes to authlib's OAuth2Session, so the"
        " tool call's refresh fails — the user is prompted to re-authorize.\n"
    )

    # Start the OAuth2 test server
    server_script = Path(__file__).parent / "oauth2_test_server.py"
    # Server rejects refresh requests that include a `scope` parameter
    # (mimics Salesforce behavior).
    server_env = {**os.environ, "STRICT_SCOPE_REJECTION": "1"}
    server_proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(server_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=server_env,
    )
    try:
        if not wait_for_server(SERVER_URL):
            print("❌ OAuth2 test server failed to start", file=sys.stderr)
            return 1

        seed = seed_credential_via_auth_code_flow()
        seeded_state = build_seeded_state(seed)
        print(
            f"🔑 Seeded credential:"
            f" access_token={seed.oauth2.access_token[:16]!r}… (expires_at=1)"
        )

        runner = InMemoryRunner(agent=agent.root_agent, app_name=APP_NAME)
        session = await runner.session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID, state=seeded_state
        )

        try:
            # Single tool call — triggers refresh. Without --apply-fix the
            # refresh fails because the server rejects `scope` in the request.
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
