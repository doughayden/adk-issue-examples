"""Agent + App for the eval-drops-App-plugins reproduction.

The ``App`` composes the root agent with a ``SentinelPlugin`` whose
``before_run_callback`` records that it fired. Under the #5503 bug, ADK's eval
inference builds its ``Runner`` from the bare ``root_agent`` and drops
``app.plugins`` entirely, so the sentinel never fires during an eval run. The
same drop is why LLM-judge metrics that read ``app_details`` (developer
instructions plus tool declarations) score an agent that was never actually
assembled with its plugins.
"""

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.plugins.base_plugin import BasePlugin

APP_NAME = "example_agent"
MODEL = "gemini-2.5-flash"

# Records every App-level plugin invocation observed during eval inference.
# main.py inspects this after the eval run: empty means the App's plugins were
# dropped (the bug); non-empty means they were applied (the fix).
PLUGIN_INVOCATIONS: list[str] = []


class SentinelPlugin(BasePlugin):
    """An App-level plugin that records when it runs inside the eval Runner."""

    async def before_run_callback(self, *, invocation_context: object) -> None:
        PLUGIN_INVOCATIONS.append(self.name)
        return None


root_agent = LlmAgent(
    name="example_agent",
    model=MODEL,
    instruction="You are a friendly assistant. Answer in one short sentence.",
)

app = App(
    name=APP_NAME,
    root_agent=root_agent,
    plugins=[SentinelPlugin(name="sentinel")],
)
