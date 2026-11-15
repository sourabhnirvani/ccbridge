"""Starts the ccbridge MCP server. bridge.py points .mcp.json here.

Claude Code starts MCP servers in your project folder. Run as
``python -m ccbridge.mcp_server``, Python would look for modules there first,
so a project with a folder named ``mcp``, ``httpx`` or ``ccbridge`` would break
the bridge (or run the project's code in its place). Run as a script, Python
looks next to this file instead.
"""

from ccbridge.mcp_server import main

if __name__ == "__main__":
    main()
