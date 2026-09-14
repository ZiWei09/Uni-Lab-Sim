"""Installed command for the ICCAS simulation runtimes."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Optional, Sequence


COMMANDS = {
    "iccas": "iccas_sim",
    "iccas-hmi": "iccas_hmi",
    "iccas-prosys": "iccas_prosys",
}


def _usage() -> str:
    return """usage: plc-sim [command] [options]

ICCAS simulator command. With no command, starts the ICCAS OPC UA simulator.

commands:
  iccas              Start ICCAS OPC UA simulation (default)
  iccas-hmi          Start two station HMI panels on localhost
  iccas-prosys       Export ICCAS NodeSets / run handshake logic on Prosys

Run `plc-sim <command> --help` for command-specific options.
"""


def _qualified_module(module_name: str) -> str:
    return f"{__package__}.{module_name}" if __package__ else module_name


def runtime_command(
    command: str,
    script_path: Path,
    args: Sequence[str] = (),
    *,
    python_executable: Optional[str] = None,
) -> list[str]:
    """Build a child-process command for source and frozen runtimes.

    PyInstaller applications do not contain a separately executable ``.py``
    entry point.  A frozen child therefore re-enters the same executable and
    lets :func:`main` dispatch the requested runtime command.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, command, *args]
    return [python_executable or sys.executable, str(script_path), *args]


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"-h", "--help"}:
        print(_usage(), end="")
        return 0
    if args and args[0] in {"-V", "--version"}:
        try:
            from . import __version__
        except ImportError:  # Direct source execution compatibility.
            from __init__ import __version__
        print(__version__)
        return 0

    command = args.pop(0) if args else "iccas"
    module_name = COMMANDS.get(command)
    if module_name is None:
        print(f"plc-sim: unknown command: {command}", file=sys.stderr)
        print("Run `plc-sim --help` to list commands.", file=sys.stderr)
        return 2

    module = importlib.import_module(_qualified_module(module_name))
    entry = getattr(module, "main", None)
    if not callable(entry):
        raise RuntimeError(f"{module.__name__} does not expose main()")

    previous_argv = sys.argv
    sys.argv = [f"plc-sim {command}", *args]
    try:
        return int(entry() or 0)
    finally:
        sys.argv = previous_argv
