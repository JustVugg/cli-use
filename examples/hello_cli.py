#!/usr/bin/env python3
"""Example: build an agent-friendly CLI with cli-use decorators.

Run:
    python examples/hello_cli.py greet --name world
    python examples/hello_cli.py greet --name world --shout
    python examples/hello_cli.py add --a 2 --b 3
"""
from cli_use import agent_tool, run_cli


@agent_tool
def greet(name: str, shout: bool = False) -> str:
    "Greet someone by name."
    msg = f"hello {name}"
    return msg.upper() if shout else msg


@agent_tool
def add(a: float, b: float) -> float:
    "Add two numbers."
    return a + b


if __name__ == "__main__":
    import sys
    sys.exit(run_cli())
