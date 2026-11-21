"""`hermes true-moa` — non-interactive CLI control (`en` default + `vi`)."""

from __future__ import annotations

import sys

from . import doctor, gate, i18n, report, slash, state


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="true_moa_command", metavar="<command>")
    for name, help_text in (
        ("on", "Arm strict protection (advisors retry, turn stops safely on outage)"),
        ("off", "Pause strict protection without unloading the plugin"),
        ("status", "Show gate status, active retry schedule, and telemetry"),
        ("retry", "Re-verify the gate against the running Hermes core"),
        ("report", "Print recent recovery events and counters"),
        ("doctor", "Run offline health and configuration diagnostics"),
    ):
        sub.add_parser(name, help=help_text)

    lang_p = sub.add_parser("lang", help="Show or set True MoA display language (`en` default or `vi`)")
    lang_p.add_argument("values", nargs="*", help="`en` (English) or `vi` (Vietnamese)")

    ladder_p = sub.add_parser("ladder", help="Show or set the retry schedule, e.g. `ladder 10,20,40` or `ladder sync`")
    ladder_p.add_argument("values", nargs="*", help="Wait steps in seconds, `sync`, or `custom <N>`")

    fb_p = sub.add_parser("fallback", help="Show or configure backup advisor failover, e.g. `fallback 2 bdd:gemini-3.8-flash`")
    fb_p.add_argument("values", nargs="*", help="`off` or `[attempts] provider:model[,provider2:model2]`")

    quorum_p = sub.add_parser("quorum", help="Show or configure minimum healthy advisors (`all` or `1..8`)")
    quorum_p.add_argument("values", nargs="*", help="`all` (require every advisor) or `<1..8>`")

    parser.set_defaults(func=handle)


def handle(args) -> int:
    action = getattr(args, "true_moa_command", None) or "status"

    if action == "lang":
        ok, msg = slash.configure_language(list(getattr(args, "values", None) or []))
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    if action == "ladder":
        ok, msg = slash.configure_ladder(list(getattr(args, "values", None) or []))
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    if action == "fallback":
        ok, msg = slash.configure_fallback(list(getattr(args, "values", None) or []))
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    if action == "quorum":
        ok, msg = slash.configure_quorum(list(getattr(args, "values", None) or []))
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    if action == "doctor":
        ok, msg = doctor.run()
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

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
        print(i18n.msg("disarmed_cli"))
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
