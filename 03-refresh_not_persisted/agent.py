"""Weather Assistant Agent.

Adapted from ADK's ``oauth2_client_credentials`` contributing sample. Two
deliberate differences:

1. Uses the ``authorization_code`` flow (not ``client_credentials``) — the
   bug we reproduce lives in the refresh code path, and
   ``client_credentials`` doesn't issue refresh_tokens.
2. Uses ``OpenAPIToolset`` (not ``AuthenticatedFunctionTool``) — the bug
   lives in ``ToolAuthHandler`` / ``ToolContextCredentialStore``, which is
   the credential path used by ``OpenAPIToolset``.
"""

from fastapi.openapi.models import OAuth2, OAuthFlowAuthorizationCode, OAuthFlows
from google.adk.agents.llm_agent import Agent
from google.adk.auth.auth_credential import (
    AuthCredential,
    AuthCredentialTypes,
    OAuth2Auth,
)
from google.adk.tools.openapi_tool import OpenAPIToolset

TOKEN_URL = "http://localhost:8080/token"  # noqa: S105
AUTH_URL = "http://localhost:8080/auth"
REDIRECT_URI = "http://localhost:8080/callback"
CLIENT_ID = "test_client"
CLIENT_SECRET = "test_secret"  # noqa: S105

# OpenAPI spec for the weather API exposed by the test server.
WEATHER_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Weather API", "version": "1.0.0"},
    "servers": [{"url": "http://localhost:8080"}],
    "paths": {
        "/api/weather": {
            "get": {
                "operationId": "get_weather",
                "parameters": [
                    {
                        "name": "city",
                        "in": "query",
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "Weather data",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        }
    },
}


def build_auth_scheme() -> OAuth2:
    return OAuth2(
        flows=OAuthFlows(
            authorizationCode=OAuthFlowAuthorizationCode(
                authorizationUrl=AUTH_URL,
                tokenUrl=TOKEN_URL,
                scopes={
                    "read": "Read access to weather data",
                    "write": "Write access for data updates",
                    "admin": "Administrative access",
                },
            )
        ),
    )


def build_auth_credential() -> AuthCredential:
    return AuthCredential(
        auth_type=AuthCredentialTypes.OAUTH2,
        oauth2=OAuth2Auth(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            token_endpoint_auth_method="client_secret_post",  # noqa: S106
        ),
    )


weather_toolset = OpenAPIToolset(
    spec_dict=WEATHER_SPEC,
    auth_scheme=build_auth_scheme(),
    auth_credential=build_auth_credential(),
)

# Disable the framework-level preemptive auth check so we can isolate the
# refresh-not-persisted bug. Without this, ADK's _resolve_toolset_auth
# triggers an OAuth redirect on every LLM invocation — even for prompts
# that would never call this tool — because the framework-level credential
# store uses a different key format than the tool-level store. This is a
# separate ADK bug that would also need to be fixed upstream; see the
# companion "Preemptive toolset auth" issue.
weather_toolset.get_auth_config = lambda: None  # type: ignore[method-assign]

root_agent = Agent(
    name="WeatherAssistant",
    description=(
        "Weather assistant that provides current weather information for cities"
        " worldwide."
    ),
    model="gemini-2.5-flash",
    instruction=(
        "You are a helpful Weather Assistant that provides current weather"
        " information for any city worldwide."
    ),
    tools=[weather_toolset],
)
