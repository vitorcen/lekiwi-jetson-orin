"""Robot motion actions: record on the board, replay from GUI or MCP.

One action = one JSON file = one source of truth. `store` owns the schema and
the filesystem, `player` owns the pure recording/playback maths, `server` and
`control` are the two ends of base_host's local Unix control socket, `cli` is
what the GUI drives over SSH and `mcp_server` is what the LLM sees.

Everything here is stdlib-only so base_host (conda lerobot env), the CLI
(/usr/bin/python3) and the MCP server (vlm venv) can all import it.
"""
