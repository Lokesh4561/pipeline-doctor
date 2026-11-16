"""MCP server that exposes the diagnostic tools over stdio.

Run it on its own:  python -m doctor.mcp_server
Any MCP client (the ADK agent here, Claude Desktop, VS Code) can then use the tools.
All tools are read-only.
"""
from mcp.server.mcpserver import MCPServer

from doctor.tools import ALL_TOOLS

server = MCPServer(
    name="pipeline-doctor",
    instructions="Read-only tools to investigate failed data pipeline runs "
    "(GitHub Actions logs, workflow files, commits, Postgres health, Databricks job runs).",
)

for fn in ALL_TOOLS:
    server.tool()(fn)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
