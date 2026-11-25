"""True MoA plugin entry point.

Registers three things and gets out of the way:

* the gate itself (see ``true_moa.gate``) — verified against the running core and
  applied in-process only, restored on unload;
* ``/true-moa`` — the live switch and status card, from the chat;
* ``hermes true-moa`` — the same from a shell.

If Hermes ever changes the internals the gate depends on, ``install()`` refuses
to patch anything and reports the mismatch through ``/true-moa status``: MoA then
behaves exactly like stock instead of running half-guarded.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    from .true_moa import cli, gate, report, slash, state

    state.migrate_legacy_state()
    report.migrate_legacy_reports()
    state.bind_context(ctx)
    state.load()
    def _cleanup() -> None:
        gate.deactivate_native_hooks()
        gate.uninstall()
        state.bind_context(None)

    try:
        ctx.on_unload(_cleanup)
    except Exception as exc:  # older core without the teardown door
        logger.debug("true-moa: no unload hook available (%s)", exc)

    is_validate_probe = getattr(ctx, "plugin_id", "") == "hermes_validate_probe_plugin"
    if not is_validate_probe and gate.native_hooks_supported():
        result = gate.activate_native_hooks(ctx)
    else:
        result = gate.install()
    if not result.get("ok"):
        logger.warning(
            "true-moa: gate inactive — %s (%s). MoA keeps stock fail-open behaviour.",
            result.get("error", "unknown"), "; ".join(result.get("missing", [])) or "no detail",
        )

    ctx.register_command(
        "true-moa",
        slash.handle,
        description="Control strict Mixture of Agents protection, smart cache optimization, and health diagnostics",
        args_hint="on|off|status|smart|cache|retry|report|doctor|lang|ladder|fallback|quorum",
    )
    ctx.register_cli_command(
        name="true-moa",
        help="Strict MoA protection & smart prefix caching: retry failed advisors, optimize cache hits, halt safely on outage",
        setup_fn=cli.setup_parser,
        handler_fn=cli.handle,
        description="Control True MoA: `hermes true-moa on|off|status|smart|cache|retry|report|doctor|lang|ladder|fallback|quorum`.",
    )

    def _classify_reference_outage(error=None, **_kwargs):
        """Make an outage a clean stop, not an unknown-error retry storm."""
        if isinstance(error, gate.MoaReferenceOutage):
            return {
                # retryable: False stops Hermes from re-backing-off after our ladder already waited.
                "reason": "unknown",
                "retryable": False,
                "should_compress": False,
                "should_rotate_credential": False,
                "should_fallback": False,
                "message": str(error),
            }
        return None

    try:
        ctx.register_hook("transform_api_error_classification", _classify_reference_outage)
    except Exception as exc:
        logger.debug("true-moa: error-classification hook unavailable (%s)", exc)
