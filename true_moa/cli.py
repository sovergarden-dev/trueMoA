"""`hermes true-moa` — the same switch from the shell.

Non-interactive by design: every action prints one block and exits, so it can be
called from a script, a watchdog, or a support session without a TTY.
"""

from __future__ import annotations

import sys

from . import gate, report, slash, state


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="true_moa_command", metavar="<command>")
    for name, help_text in (
        ("on", "Arm the gate (references retry, turn halts on outage)"),
        ("off", "Disarm without unloading the plugin"),
        ("status", "Show gate state, ladder and compatibility"),
        ("retry", "Re-verify the gate against the running core"),
        ("report", "Print the recovery log tail and counters"),
        ("ladder", "Show or set the retry ladder, e.g. `ladder 10,20,40`"),
    ):
        sub.add_parser(name, help=help_text)
    parser.set_defaults(func=handle)


def handle(args) -> int:
    action = getattr(args, "true_moa_command", None) or "status"

    if action == "ladder":
        current = state.effective_ladder()
        print("true-moa ladder: " + " → ".join(f"{item:g}s" for item in current) + " → halt")
        return 0

    if action == "on":
        state.set_enabled(True)
        result = gate.install()
        if not result.get("ok"):
            print("✗ gate could not arm: " + str(result.get("error", "")), file=sys.stderr)
            for item in result.get("missing", []):
                print(f"  · {item}", file=sys.stderr)
            return 1
        report.event("gate_armed", source="cli")
        print(slash.handle("status"))
        return 0

    if action == "off":
        state.set_enabled(False)
        report.event("gate_disarmed", source="cli")
        print("True MoA disarmed — stock fail-open restored. `/true-moa on` re-arms instantly.")
        return 0

    if action == "retry":
        result = gate.install(force=True)
        print(slash.handle("status"))
        return 0 if result.get("ok") else 1

    if action == "report":
        print(slash.handle("report"))
        return 0

    print(slash.handle("status"))
    return 0
