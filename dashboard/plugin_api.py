"""FastAPI REST endpoints for the True MoA Desktop Settings UI (`/api/plugins/true-moa/*`)."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _PLUGIN_ROOT / "true_moa"
_PKG_INIT = (_PKG_DIR / "__init__.py").resolve()


def _resolve_true_moa_package() -> str:
    for mod_name, mod_obj in list(sys.modules.items()):
        mod_file = getattr(mod_obj, "__file__", None)
        if not mod_file:
            continue
        try:
            if Path(mod_file).resolve() == _PKG_INIT:
                return mod_name
        except OSError:
            continue
    pkg_name = "_true_moa_dashboard_pkg"
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        _PKG_INIT,
        submodule_search_locations=[str(_PKG_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not create module spec for {_PKG_INIT}")
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = pkg
    spec.loader.exec_module(pkg)
    return pkg_name


_PKG = _resolve_true_moa_package()
doctor = importlib.import_module(f"{_PKG}.doctor")
gate = importlib.import_module(f"{_PKG}.gate")
i18n = importlib.import_module(f"{_PKG}.i18n")
report = importlib.import_module(f"{_PKG}.report")
state = importlib.import_module(f"{_PKG}.state")


class SettingsPatchBody(BaseModel):
    language: str | None = None
    enabled: bool | None = None
    block_aggregator_fallback: bool | None = None
    min_healthy_references: int | None = None
    sync_hermes_retries: bool | None = None
    custom_retries: int | None = None
    ladder: list[float] | str | None = None
    max_wait_budget_s: float | None = None
    enable_secondary_fallback: bool | None = None
    secondary_reference_model: str | None = None
    fallback_trigger_attempts: int | None = None


def build_dashboard_snapshot() -> dict[str, Any]:
    """Return current settings, active retry schedule, gate health, doctor report, and counters."""
    cfg = state.load()
    lang = state.normalize_language(cfg.get("language"))
    src, attempts = state.ladder_source(cfg)
    eff_ladder = state.effective_ladder(cfg)
    return {
        "settings": cfg,
        "schedule": {
            "source": src,
            "attempts": attempts,
            "effective_ladder": eff_ladder,
            "total_sleep_s": round(sum(eff_ladder), 2),
        },
        "status": gate.status(),
        "doctor": doctor.run_structured(lang=lang),
        "telemetry": report.summary(),
        "recent": report.tail(8),
    }


def apply_settings_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist a partial settings update, returning the refreshed snapshot."""
    current = state.load()
    lang = state.normalize_language(patch.get("language") or current.get("language"))
    clean_patch = {k: v for k, v in patch.items() if k in state.DEFAULTS and v is not None}

    if "secondary_reference_model" in clean_patch:
        raw_sec = str(clean_patch["secondary_reference_model"]).strip()
        if raw_sec:
            entries = [part.strip() for part in raw_sec.split(",") if part.strip()]
            for entry in entries:
                parsed = state.parse_reference_model_string(entry)
                if parsed is None:
                    raise ValueError(i18n.msg("fb_entry_invalid", lang=lang, raw=entry))
                if parsed["provider"].lower() == "moa":
                    raise ValueError(i18n.msg("fb_moa_forbidden", lang=lang))
            chain = state.parse_reference_model_chain(raw_sec)
            clean_patch["secondary_reference_model"] = ", ".join(
                f"{slot['provider']}:{slot['model']}" for slot in chain
            )
        else:
            clean_patch["secondary_reference_model"] = ""

    if clean_patch:
        saved = state.save(clean_patch)
        if clean_patch.get("enabled") is True and not gate.status().get("installed"):
            gate.install()
        lang = state.normalize_language(saved.get("language"))
    return build_dashboard_snapshot()


router = APIRouter()


@router.get("/state")
def api_get_state() -> dict[str, Any]:
    return build_dashboard_snapshot()


@router.post("/settings")
def api_patch_settings(body: SettingsPatchBody) -> dict[str, Any]:
    try:
        return apply_settings_patch(body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/doctor")
def api_run_doctor() -> dict[str, Any]:
    return build_dashboard_snapshot()


@router.post("/reverify")
def api_reverify_gate() -> dict[str, Any]:
    res = gate.install(force=True)
    snap = build_dashboard_snapshot()
    snap["reverify"] = res
    return snap
