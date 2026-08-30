"""Shared plumbing for the deployment checks.

Deliberately dependency-light and deliberately not a test framework: these are
run against a deployment you are about to trust, often from a laptop that is
not the machine the app is on, and the useful output is a list of lines a human
reads once. Anything a check needs beyond the standard library
(``aiohttp``, ``python-socketio``) is already in the backend's own environment.
"""

from __future__ import annotations

import sys

# ANSI, but only when something is going to render it.
_TTY = sys.stdout.isatty()
_GREEN, _RED, _DIM, _BOLD, _OFF = (
    ("\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m") if _TTY else ("",) * 5
)


class Report:
    """Collects pass/fail lines and decides the exit code."""

    def __init__(self, title: str) -> None:
        self.failures: list[str] = []
        print(f"\n{_BOLD}{title}{_OFF}")

    def section(self, name: str) -> None:
        print(f"\n  {_BOLD}{name}{_OFF}")

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        """Record one result. Returns ``ok``, so it can gate what follows."""
        mark = f"{_GREEN}PASS{_OFF}" if ok else f"{_RED}FAIL{_OFF}"
        suffix = f" {_DIM}— {detail}{_OFF}" if detail else ""
        print(f"    {mark}  {name}{suffix}")
        if not ok:
            self.failures.append(name)
        return ok

    def note(self, text: str) -> None:
        print(f"    {_DIM}{text}{_OFF}")

    def finish(self) -> int:
        """Print the verdict and return the exit code to leave with."""
        print()
        if self.failures:
            print(f"  {_RED}{_BOLD}FAILED{_OFF}: {', '.join(self.failures)}\n")
            return 1
        print(f"  {_GREEN}{_BOLD}All checks passed{_OFF}\n")
        return 0


def die(message: str) -> None:
    """Stop with a message, for the things a check cannot work around."""
    print(f"\n  {_RED}Cannot run:{_OFF} {message}\n", file=sys.stderr)
    raise SystemExit(2)
