"""WeatherAssistant Agent main script (redirect_uri-in-hash repro).

Reproduces an ADK bug where ``ToolContextCredentialStore.get_credential_key``
hashes ``redirect_uri`` along with the rest of the OAuth2 credential.
Two credentials that share the same OAuth identity (client_id,
client_secret, scopes, tokens) but differ only in ``redirect_uri`` —
for example, the local-relay URL vs. the deployed-relay URL — produce
different hash keys, so a credential minted under one redirect_uri is
no longer retrievable when the deployment moves to the other.

Differences from ``03-refresh_not_persisted/main.py``:

1. The seed credential is built with ``STORED_REDIRECT_URI``, while
   ``agent.weather_toolset`` is built with ``CURRENT_REDIRECT_URI``.
   The agent's runtime hash differs from the seed's stored key.
2. The seed credential's ``access_token`` is real (not expired) — the
   bug we demonstrate is the lookup miss, not refresh behavior. With
   the fix applied, the tool call should succeed without any refresh.
3. ``--apply-fix`` patches BOTH ``get_credential_key`` and
   ``_get_legacy_credential_key`` on
   ``ToolContextCredentialStore`` to strip ``redirect_uri`` before
   delegating to ADK's original hashing.

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
from pathlib import Path

import httpx
from dotenv import load_dotenv
from google.adk.agents.run_config import RunConfig
from google.adk.auth.auth_credential import (
    AuthCredential,
    AuthCredentialTypes,
    OAuth2Auth,
)
from google.adk.auth.auth_schemes import AuthScheme
from google.adk.auth.auth_tool import AuthConfig
from google.adk.cli.utils import logs
from google.adk.runners import InMemoryRunner
from google.adk.tools.openapi_tool.openapi_spec_parser.tool_auth_handler import (
    ToolContextCredentialStore,
)
from google.genai import types

import agent


def apply_proposed_fix() -> None:
    """Apply the proposed upstream fix: strip redirect_uri before hashing.

    Wraps ``ToolContextCredentialStore.get_credential_key`` and
    ``ToolContextCredentialStore._get_legacy_credential_key`` so that
    ``redirect_uri`` is pre-stripped from the credential before
    delegating to the original implementation. Mirrors the proposed
    upstream patch (a single-line addition to each strip block).

    ``redirect_uri`` is deployment configuration (which callback URL
    the auth server should redirect to), not part of the credential
    identity (the user's actual OAuth grant). Including it in the hash
    defeats the credential store's purpose across deployment-URL
    changes.
    """

    original_get_credential_key = ToolContextCredentialStore.get_credential_key
    original_get_legacy_credential_key = (
        ToolContextCredentialStore._get_legacy_credential_key
    )

    def _strip_redirect_uri(
        auth_credential: AuthCredential | None,
    ) -> AuthCredential | None:
        if auth_credential is None or auth_credential.oauth2 is None:
            return auth_credential
        copy = auth_credential.model_copy(deep=True)
        if copy.oauth2 is not None:
            copy.oauth2.redirect_uri = None
        return copy

    def patched_get_credential_key(
        self: ToolContextCredentialStore,
        auth_scheme: AuthScheme | None,
        auth_credential: AuthCredential | None,
    ) -> str:
        return original_get_credential_key(
            self, auth_scheme, _strip_redirect_uri(auth_credential)
        )

    def patched_get_legacy_credential_key(
        self: ToolContextCredentialStore,
        auth_scheme: AuthScheme | None,
        auth_credential: AuthCredential | None,
    ) -> str:
        return original_get_legacy_credential_key(
            self, auth_scheme, _strip_redirect_uri(auth_credential)
        )

    ToolContextCredentialStore.get_credential_key = (  # type: ignore[method-assign]
        patched_get_credential_key
    )
    ToolContextCredentialStore._get_legacy_credential_key = (  # type: ignore[method-assign]
        patched_get_legacy_credential_key
    )


APP_NAME = "weather_assistant_app"
USER_ID = "weather_user"
SERVER_URL = "http://127.0.0.1:8080"

logs.setup_adk_logger(level=logging.ERROR)


def process_arguments() -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Weather Assistant Agent — reproduces ADK's"
            " redirect_uri-in-credential-hash bug. The seed credential is"
            " stored under one redirect_uri's hash, the agent looks up under"
            " another redirect_uri's hash, lookup misses, agent prompts"
            " for re-auth despite the credential being present."
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
            "Monkey-patch the proposed upstream fix (strip redirect_uri"
            " from the credential before hashing)."
        ),
    )
    return parser.parse_args()


async def run_and_log(runner, session_id: str, message: str) -> dict[str, int]:
    """Run the agent and log every event with category counters.

    Returns counts of events observed by category. The bug shows up as
    ``auth_events > 0`` (the agent emitted ``adk_request_credential``).
    The fix shows up as ``auth_events == 0`` plus
    ``function_responses > 0`` (the tool call succeeded with the seeded
    credential).
    """

    print(f"\n👤 User: {message}")
    print("🌤️  Weather Assistant event stream:\n")

    content = types.Content(role="user", parts=[types.Part.from_text(text=message)])
    counts = {
        "function_calls": 0,
        "auth_events": 0,
        "function_responses": 0,
        "text_events": 0,
    }

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
                print(f"    [text] {event.author}: {preview!r}")

    return counts


def seed_credential_via_auth_code_flow() -> AuthCredential:
    """Complete the authorization_code flow against the local server.

    The OAuth flow's ``redirect_uri`` must match the server's allow-list
    (``STORED_REDIRECT_URI`` — see ``oauth2_test_server.py``). The
    resulting ``AuthCredential`` is constructed with the same value, so
    the seed credential's identity (client_id + secret + scopes +
    redirect_uri) matches what a real local-relay deployment would have
    minted.
    """

    auth_resp = httpx.get(
        agent.AUTH_URL,
        params={
            "response_type": "code",
            "client_id": agent.CLIENT_ID,
            "redirect_uri": agent.STORED_REDIRECT_URI,
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
            "redirect_uri": agent.STORED_REDIRECT_URI,
        },
    )
    token_resp.raise_for_status()
    tokens = token_resp.json()

    # access_token left unexpired (no expires_at override) so the bug
    # manifests as a pure lookup miss, not as a refresh failure.
    return AuthCredential(
        auth_type=AuthCredentialTypes.OAUTH2,
        oauth2=OAuth2Auth(
            client_id=agent.CLIENT_ID,
            client_secret=agent.CLIENT_SECRET,
            redirect_uri=agent.STORED_REDIRECT_URI,
            token_endpoint_auth_method="client_secret_post",  # noqa: S106
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
        ),
    )


def compare_hash_keys() -> tuple[str, str]:
    """Compute the credential-store hash key for both redirect_uri values.

    Demonstrates the bug at the hash-key level before running the agent.
    With the fix monkey-patched in, both keys should be identical (the
    redirect_uri is stripped before delegating to ADK's original
    ``get_credential_key``).
    """

    auth_scheme = agent.build_auth_scheme()
    store = ToolContextCredentialStore(tool_context=None)

    stored_cred = agent.build_auth_credential(agent.STORED_REDIRECT_URI)
    current_cred = agent.build_auth_credential(agent.CURRENT_REDIRECT_URI)

    return (
        store.get_credential_key(auth_scheme, stored_cred),
        store.get_credential_key(auth_scheme, current_cred),
    )


def build_seeded_state(seed: AuthCredential) -> dict:
    """Pre-populate session state under the STORED redirect_uri's hash key.

    The seed credential's ``redirect_uri`` is ``STORED_REDIRECT_URI``,
    so the tool-level credential store key is computed against that
    value. The agent (built with ``CURRENT_REDIRECT_URI``) will compute
    a different key at lookup time, miss, and prompt for re-auth.

    Also seeds the framework-level key for the same credential —
    matches the defensive pattern in
    ``03-refresh_not_persisted/main.py`` even though ``agent.py``'s
    ``get_auth_config = lambda: None`` disables the framework-level
    path here.
    """

    auth_scheme = agent.build_auth_scheme()
    stored_cred = agent.build_auth_credential(agent.STORED_REDIRECT_URI)
    seed_dict = seed.model_dump(exclude_none=True)

    tool_store = ToolContextCredentialStore(tool_context=None)
    tool_key = tool_store.get_credential_key(auth_scheme, stored_cred)

    framework_key = AuthConfig(
        auth_scheme=auth_scheme, raw_auth_credential=stored_cred
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

    print("🌤️  WeatherAssistant Agent — redirect_uri-in-hash repro")
    print("=" * 60)
    print(f"Proposed fix applied:      {args.apply_fix}")
    print(f"STORED_REDIRECT_URI:       {agent.STORED_REDIRECT_URI}")
    print(f"CURRENT_REDIRECT_URI:      {agent.CURRENT_REDIRECT_URI}")

    stored_key, current_key = compare_hash_keys()
    print("\nHash keys produced by ToolContextCredentialStore.get_credential_key:")
    print(f"    STORED   → {stored_key}")
    print(f"    CURRENT  → {current_key}")
    if stored_key == current_key:
        print("    ✅ Keys match — fix is taking effect at the hash level.")
    else:
        print(
            "    ❌ Keys differ — credentials minted under STORED are not retrievable."
        )

    print(
        "\nSeeding a real (non-expired) credential into session state under"
        " STORED_REDIRECT_URI's hash, then running the agent (built with"
        " CURRENT_REDIRECT_URI). Without the fix, the agent's runtime hash"
        " misses the seeded key and prompts for re-auth even though the"
        " credential is present and valid.\n"
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

        seed = seed_credential_via_auth_code_flow()
        seeded_state = build_seeded_state(seed)
        if seed.oauth2 is None or seed.oauth2.access_token is None:
            print("❌ Seed credential is missing tokens", file=sys.stderr)
            return 1
        print(
            f"🔑 Seeded credential:"
            f" access_token={seed.oauth2.access_token[:16]!r}…"
            f" (redirect_uri={seed.oauth2.redirect_uri!r})"
        )

        runner = InMemoryRunner(agent=agent.root_agent, app_name=APP_NAME)
        session = await runner.session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID, state=seeded_state
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
            if counts["auth_events"] == 0 and counts["function_responses"] > 0:
                print(
                    "✅ Fix verified: tool call succeeded against the seeded"
                    " credential without an adk_request_credential prompt."
                )
                return 0
            print(
                "❌ Fix did not take effect:"
                f" auth_events={counts['auth_events']},"
                f" function_responses={counts['function_responses']}."
            )
            return 1
        if counts["auth_events"] > 0:
            print(
                "✅ Bug reproduced: agent emitted"
                f" {counts['auth_events']} adk_request_credential event(s)"
                " despite a valid seeded credential being present in state"
                " (hashed under STORED_REDIRECT_URI; agent looked under"
                " CURRENT_REDIRECT_URI)."
            )
            return 0
        print(
            "⚠️  Bug not reproduced: no adk_request_credential event. The"
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
