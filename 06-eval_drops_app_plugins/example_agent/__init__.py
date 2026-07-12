"""Agent package for the eval-drops-App-plugins reproduction.

Exposes the ``agent`` submodule so ADK's ``AgentEvaluator`` can resolve
``example_agent.agent.root_agent`` (and ``example_agent.agent.app``) from the
module name ``example_agent``.
"""

from . import agent

__all__ = ["agent"]
