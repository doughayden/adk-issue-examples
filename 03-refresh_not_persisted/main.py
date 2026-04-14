"""WeatherAssistant Agent main script.

Reproduces the ADK refresh-not-persisted bug using the same agent-runtime
scaffolding as the upstream ``oauth2_client_credentials`` sample.

Differences from the upstream sample:

1. Starts the adapted ``oauth2_test_server`` as a subprocess (port 8080)
   and tears it down on exit.
2. Uses the ``authorization_code`` flow (not ``client_credentials``) so
   that refresh_tokens are issued — the bug lives in the refresh path.
3. Sends TWO messages in the same session to force two tool calls — the
   first triggers refresh (succeeds), the second exposes the bug (store
   still holds pre-refresh tokens → re-auth prompt).

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
from google.adk.auth.auth_credential import (
    AuthCredential,
    AuthCredentialTypes,
    OAuth2Auth,
)
from google.adk.auth.auth_tool import AuthConfig
from google.adk.auth.refresher.oauth2_credential_refresher import (
    OAuth2CredentialRefresher,
)
from google.adk.cli.utils import logs
from google.adk.runners import InMemoryRunner
from google.adk.tools.openapi_tool.openapi_spec_parser import tool_auth_handler
from google.adk.tools.openapi_tool.openapi_spec_parser.tool_auth_handler import (
    ToolContextCredentialStore,
)
from google.genai import types

import agent


def apply_proposed_fix() -> None:
    """Apply the proposed upstream fix: persist the credential after refresh.

    Patches ``ToolAuthHandler._get_existing_credential`` to call
    ``self._store_credential(existing_credential)`` after a successful refresh.
    This is the one-line fix proposed in the issue.
    """

    async def _get_existing_credential_patched(self):  # type: ignore[no-untyped-def]
        if not self.credential_store:
            return None
        existing = self.credential_store.get_credential(
            self.auth_scheme, self.auth_credential
        )
        if not existing:
            return None
        if existing.oauth2:
            refresher = OAuth2CredentialRefresher()
            if await refresher.is_refresh_needed(existing):
                refreshed = await refresher.refresh(existing, self.auth_scheme)
                self._store_credential(refreshed)  # ← the fix
                return refreshed
        return existing

    tool_auth_handler.ToolAuthHandler._get_existing_credential = (  # type: ignore[method-assign]
        _get_existing_credential_patched
    )


APP_NAME = "weather_assistant_app"
USER_ID = "weather_user"
SERVER_URL = "http://127.0.0.1:8080"

logs.setup_adk_logger(level=logging.ERROR)


def process_arguments() -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Weather Assistant Agent — reproduces ADK's refresh-not-persisted"
            " bug by issuing two weather queries in the same session."
        ),
        epilog=("Example usage:\n\tpython main.py\n\tpython main.py --apply-fix\n"),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--first",
        type=str,
        default="What's the weather in Tokyo?",
        help="First prompt (default: Tokyo).",
    )
    parser.add_argument(
        "--second",
        type=str,
        default="And the weather in London?",
        help="Second prompt — triggers the bug (default: London).",
    )
    parser.add_argument(
        "--apply-fix",
        action="store_true",
        help=(
            "Monkey-patch the proposed upstream fix (persist refreshed "
            "credential to the store). With this flag, both queries should "
            "succeed. Without it, the second query triggers re-auth."
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
    isolate the refresh-not-persisted bug.
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

    print("🌤️  WeatherAssistant Agent — refresh-not-persisted repro")
    print("=" * 60)
    print(f"Proposed fix applied: {args.apply_fix}")
    print(
        "Sending two weather queries in the same session. Under the bug,"
        " the second query will trigger a full re-auth because the first"
        " refresh was never persisted to the credential store.\n"
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
            # First tool call — should succeed after initial auth flow.
            await process_message(runner, session.id, args.first)
            # Second tool call — exposes the bug (stale store → re-auth).
            await process_message(runner, session.id, args.second)
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
