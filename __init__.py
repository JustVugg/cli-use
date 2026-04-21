"""cli-use: create agent-friendly CLIs and convert MCP servers into CLIs.

Two primary uses:

    from cli_use import agent_tool, run_cli

    @agent_tool
    def greet(name: str, shout: bool = False) -> str:
        "Greet someone."
        msg = f"hello {name}"
        return msg.upper() if shout else msg

    if __name__ == "__main__":
        run_cli()

And from the command line:

    cli-use convert <mcp-server-cmd> --out ./my-cli.py
"""

from cli_use.create import agent_tool, run_cli

__all__ = ["agent_tool", "run_cli"]
__version__ = "0.0.1"
