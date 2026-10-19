"""An agent in the conversation as NCTE's model: the task queue, the engine process it feeds, and the MCP server.

For a run on the user's Claude plan without a conversation in the loop, the ``claude`` provider (``llm/claude_cli``)
calls Claude Code directly; this package is for an agent that answers the engine's requests itself.
"""
