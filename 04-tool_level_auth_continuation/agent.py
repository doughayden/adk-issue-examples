"""Weather Assistant Agent (tool-level auth variant).

Same agent shape as ``01-preemptive_toolset_auth/agent.py``: an
``OpenAPIToolset`` with OAuth2 ``authorization_code`` flow pointed at
the local test server's ``/api/weather`` endpoint. The bug we reproduce
here is downstream of #5327's workaround, so the agent definition is
unchanged and ``main.py`` applies the workaround at runtime to put us
on the tool-level auth path.
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

root_agent = Agent(
    name="WeatherAssistant",
    description=(
        "Weather assistant that provides current weather information for cities"
        " worldwide."
    ),
    model="gemini-2.5-flash",
    instruction=(
        "You are a helpful Weather Assistant that provides current weather"
        " information for any city worldwide. When asked about weather, call"
        " the get_weather tool."
    ),
    tools=[weather_toolset],
)
