"""Bilingual message catalog (English default + Vietnamese) for True MoA."""

from __future__ import annotations

from typing import Any

from . import state

SUPPORTED_LANGUAGES = state.SUPPORTED_LANGUAGES
DEFAULT_LANGUAGE = state.DEFAULT_LANGUAGE
is_supported_language = state.is_supported_language
normalize_language = state.normalize_language

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "usage": (
            "usage: /true-moa on | off | status | retry | report | doctor | "
            "lang [en | vi] | ladder [sync | custom <N> | <s1,s2,...>] | "
            "fallback [off | [<attempts>] <provider:model[,...]>] | quorum [all | <1..8>]"
        ),
        "lang_current": "true-moa language: English (en). Switch with `/true-moa lang vi`.",
        "lang_set": "True MoA language set to English (en).",
        "lang_invalid": "usage: /true-moa lang [en | vi]",
        "status_armed": "ARMED",
        "status_standby": "STANDBY (stock fail-open)",
        "status_uninstalled": "NOT INSTALLED",
        "status_mode_native": "native hooks",
        "status_mode_seams": "{count} seam(s)",
        "status_quorum_all": "all enabled advisors required",
        "status_quorum_min": "at least {count} healthy advisor(s)",
        "status_blocked": "blocked (main model never impersonates advisors)",
        "status_allowed": "allowed",
        "status_fb_enabled": "switches to {models} after {attempts} failure(s)",
        "status_fb_disabled": "disabled (primary advisors only)",
        "status_cache_unconditional": "active (failed advisor responses are never cached)",
        "status_budget_cap": " (cap {budget:g}s)",
        "status_budget_unbounded": " (full schedule)",
        "status_block": (
            "True MoA — {headline}\n"
            "  language        English (en)\n"
            "  core            {core_version} (requires {target_core})\n"
            "  gate            v{plugin_version} · {mode_tag}: {wrapped}\n"
            "  retry schedule  {ladder} → halt{budget_label} [{source}: {attempts} attempts]\n"
            "  wait budget     {budget:g}s sleep cap (active in-flight requests are never cut off)\n"
            "  quorum          {quorum_desc}\n"
            "  self-answering  {impersonation}\n"
            "  backup advisor  {fallback_desc}\n"
            "  cache guard     {cache_desc}\n"
            "  activity ({days}d)   retries {retries} · backup switches {switches} · quorum {quorum_hits} · halted outages {outages} · blocked impersonations {blocked}"
        ),
        "status_compat_failed": "  compatibility   FAILED — {error}",
        "armed_ok": "True MoA armed. Failed advisors retry ({ladder}) and the turn stops safely if they cannot recover.",
        "armed_fail": "True MoA could not arm: {error}\n{missing}\nMoA continues with Hermes' default fail-open behavior.",
        "disarmed_ok": "True MoA paused. Hermes reverts to default fail-open behavior; run `/true-moa on` to re-arm anytime.",
        "disarmed_cli": "True MoA paused — default fail-open restored. Run `/true-moa on` to re-arm anytime.",
        "reverified_seams": "Gate re-verified on Hermes {core_version} — {count} seam(s) active.",
        "reverified_native": "Gate verified on Hermes {core_version} — native hooks active ({wrapped}).",
        "reverify_fail": "Re-verification failed: {error}\n{missing}",
        "report_header": "True MoA activity ({days} day(s)) — counters {counters}\n{body}",
        "report_empty": "  (no recovery events recorded yet)",
        "ladder_show": "true-moa retry schedule: {ladder} → halt [{source}: {attempts} attempts]",
        "ladder_synced": "true-moa retry schedule synced to Hermes API Retries ({attempts} attempts): {ladder} → halt",
        "ladder_custom_usage": "usage: /true-moa ladder custom <0..25>",
        "ladder_custom_invalid": "invalid attempt count {raw!r} (enter a whole number from 0 to 25)",
        "ladder_custom_range": "attempt count must be between 0 and 25",
        "ladder_custom_set": "true-moa retry schedule updated [{source}: {attempts} attempts]: {ladder} → halt",
        "ladder_step_invalid": "invalid wait step {raw!r} (enter seconds like `15,30,60`, `sync`, or `custom <N>`)",
        "ladder_step_finite": "wait step {raw!r} must be a non-negative number of seconds",
        "ladder_usage": "usage: /true-moa ladder [sync | custom <N> | <s1,s2,...>]",
        "ladder_manual_set": "true-moa custom retry schedule set ({attempts} attempts): {ladder} → halt",
        "fb_show_enabled": "true-moa backup advisor fallback: enabled → {models} after {attempts} failure(s)",
        "fb_show_disabled": "true-moa backup advisor fallback: disabled",
        "fb_off": "true-moa backup advisor fallback disabled.",
        "fb_trigger_range": "switch threshold must be between 1 and 10 failed attempts",
        "fb_spec_invalid": "invalid backup model (expected `provider:model` or `p1:m1, p2:m2`)",
        "fb_entry_invalid": "invalid backup model entry {raw!r} (expected `provider:model`)",
        "fb_moa_forbidden": "backup advisor cannot use the recursive `moa` provider",
        "fb_armed": "true-moa backup advisor fallback armed → {models} after {attempts} failure(s).",
        "quorum_show": "true-moa quorum: {label}",
        "quorum_all_set": "true-moa quorum set to strict `all` (every enabled advisor must succeed).",
        "quorum_usage": "usage: /true-moa quorum [all | <1..8>]",
        "quorum_range": "quorum count must be between 1 and 8 (or `all`)",
        "quorum_set": "true-moa quorum set: requires at least {count} healthy advisor(s) after retries.",
        "progress_retry": "Waiting {delay:g}s to retry {models} (attempt {attempt}/{total})",
        "progress_switched": "Switched failed advisor to backup {secondary}",
        "outage_ladder_exhausted": " (True MoA ladder={ladder} exhausted)",
        "outage_budget_reached": " (True MoA sleep budget reached)",
        "outage_message": (
            "MoA reference outage: {names} failed {attempts} attempt(s) over {waited:.0f}s{ladder_desc}. "
            "Turn halted before the aggregator acted; no reference result was cached."
        ),
        "doctor_title": "True MoA Doctor — Offline Health Check",
        "doctor_verdict_healthy": "Verdict: HEALTHY",
        "doctor_verdict_warnings": "Verdict: HEALTHY (with warnings)",
        "doctor_verdict_issues": "Verdict: ISSUES FOUND ({count})",
        "doctor_gate_ok": "v{plugin_version} on Hermes {core_version} ({seam_desc})",
        "doctor_gate_fail": "inactive — {error}",
        "doctor_pm_ok": "tooling-only (installs_packages=False, plugin dir clean)",
        "doctor_pm_fail": "workspace stamp hazard detected ({detail})",
        "doctor_data_ok": "{path}",
        "doctor_data_fail": "invalid storage state at {path}",
        "doctor_preset_none": "none configured in config.yaml",
        "doctor_preset_zero": "'{preset}' has 0 enabled advisors",
        "doctor_preset_warn": "'{preset}' ({count} advisor(s)): {problems}",
        "doctor_preset_ok": "'{preset}' ({count} enabled reference(s), credentials resolved)",
        "doctor_fb_empty": "enabled with empty or malformed secondary_reference_model",
        "doctor_fb_collision": "backup model collides with aggregator or uses 'moa'",
        "doctor_fb_warn": "{count} backup(s): {problems}",
        "doctor_fb_ok": "{labels} (after {attempts} failure(s))",
        "doctor_fb_off": "disabled (primary references only)",
        "doctor_ladder_warn": "budget {budget:g}s caps even first step {first:g}s",
        "doctor_ladder_hb": " (liveness heartbeat active)",
        "doctor_ladder_ok": "{attempts} attempt(s), max sleep {total_sleep:g}s{hb_note}",
        "doctor_quorum_all": "all enabled references required",
        "doctor_quorum_min": "minimum {count} healthy reference(s)",
    },
    "vi": {
        "usage": (
            "cú pháp: /true-moa on | off | status | retry | report | doctor | "
            "lang [en | vi] | ladder [sync | custom <N> | <s1,s2,...>] | "
            "fallback [off | [<số_lần>] <provider:model[,...]>] | quorum [all | <1..8>]"
        ),
        "lang_current": "Ngôn ngữ True MoA: Tiếng Việt (vi). Đổi sang tiếng Anh bằng `/true-moa lang en`.",
        "lang_set": "Đã chuyển ngôn ngữ True MoA sang Tiếng Việt (vi).",
        "lang_invalid": "cú pháp: /true-moa lang [en | vi]",
        "status_armed": "ĐANG BẬT (Bảo vệ nghiêm ngặt)",
        "status_standby": "TẠM TẮT (Mặc định fail-open)",
        "status_uninstalled": "CHƯA KÍCH HOẠT",
        "status_mode_native": "hook chính thức",
        "status_mode_seams": "{count} điểm chặn",
        "status_quorum_all": "bắt buộc tất cả cố vấn đang bật đều thành công",
        "status_quorum_min": "tối thiểu {count} cố vấn phản hồi khỏe",
        "status_blocked": "đã chặn (mô hình chính không bao giờ tự đóng giả cố vấn)",
        "status_allowed": "cho phép",
        "status_fb_enabled": "chuyển sang {models} sau {attempts} lần lỗi",
        "status_fb_disabled": "tắt (chỉ dùng các cố vấn chính)",
        "status_cache_unconditional": "đang bật (không bao giờ lưu kết quả lỗi vào bộ nhớ đệm)",
        "status_budget_cap": " (trần chờ {budget:g}s)",
        "status_budget_unbounded": " (dùng trọn lịch thử lại)",
        "status_block": (
            "True MoA — {headline}\n"
            "  ngôn ngữ        Tiếng Việt (vi)\n"
            "  lõi hermes      {core_version} (yêu cầu {target_core})\n"
            "  cổng bảo vệ     v{plugin_version} · {mode_tag}: {wrapped}\n"
            "  lịch thử lại    {ladder} → dừng{budget_label} [{source}: {attempts} lần gọi]\n"
            "  trần thời gian  tối đa {budget:g}s nghỉ giữa các lần thử (không cắt ngang yêu cầu đang chạy)\n"
            "  ngưỡng cố vấn   {quorum_desc}\n"
            "  chống tự trả lời {impersonation}\n"
            "  cố vấn dự phòng {fallback_desc}\n"
            "  bảo vệ bộ đệm   {cache_desc}\n"
            "  nhật ký ({days} ngày)  thử lại {retries} · chuyển dự phòng {switches} · quorum {quorum_hits} · dừng an toàn {outages} · chặn đóng giả {blocked}"
        ),
        "status_compat_failed": "  tương thích     LỖI — {error}",
        "armed_ok": "Đã bật True MoA. Cố vấn gặp lỗi sẽ được thử lại ({ladder}) và dừng lượt an toàn nếu không thể hồi phục.",
        "armed_fail": "Không thể bật True MoA: {error}\n{missing}\nMoA tạm giữ hành vi mặc định của Hermes.",
        "disarmed_ok": "Đã tạm tắt True MoA. Hermes quay về chế độ mặc định; gõ `/true-moa on` để bật lại bất cứ lúc nào.",
        "disarmed_cli": "Đã tạm tắt True MoA — quay về chế độ mặc định. Gõ `/true-moa on` để bật lại bất cứ lúc nào.",
        "reverified_seams": "Đã xác minh lại cổng trên Hermes {core_version} — {count} điểm chặn đang hoạt động.",
        "reverified_native": "Đã xác minh cổng trên Hermes {core_version} — chế độ native hooks đang hoạt động ({wrapped}).",
        "reverify_fail": "Xác minh lại thất bại: {error}\n{missing}",
        "report_header": "Nhật ký True MoA ({days} ngày) — bộ đếm {counters}\n{body}",
        "report_empty": "  (chưa ghi nhận sự kiện khôi phục nào)",
        "ladder_show": "lịch thử lại true-moa: {ladder} → dừng [{source}: {attempts} lần gọi]",
        "ladder_synced": "đã đồng bộ lịch thử lại theo API Retries của Hermes ({attempts} lần gọi): {ladder} → dừng",
        "ladder_custom_usage": "cú pháp: /true-moa ladder custom <0..25>",
        "ladder_custom_invalid": "số lần thử không hợp lệ {raw!r} (nhập số nguyên từ 0 đến 25)",
        "ladder_custom_range": "số lần thử phải nằm trong khoảng từ 0 đến 25",
        "ladder_custom_set": "đã cập nhật lịch thử lại [{source}: {attempts} lần gọi]: {ladder} → dừng",
        "ladder_step_invalid": "quãng nghỉ không hợp lệ {raw!r} (nhập số giây như `15,30,60`, `sync`, hoặc `custom <N>`)",
        "ladder_step_finite": "quãng nghỉ {raw!r} phải là số giây không âm",
        "ladder_usage": "cú pháp: /true-moa ladder [sync | custom <N> | <s1,s2,...>]",
        "ladder_manual_set": "đã đặt lịch thử lại thủ công ({attempts} lần gọi): {ladder} → dừng",
        "fb_show_enabled": "cố vấn dự phòng true-moa: đang bật → {models} sau {attempts} lần lỗi",
        "fb_show_disabled": "cố vấn dự phòng true-moa: đang tắt",
        "fb_off": "Đã tắt chế độ chuyển mạch sang cố vấn dự phòng.",
        "fb_trigger_range": "ngưỡng chuyển mạch phải từ 1 đến 10 lần thử lỗi",
        "fb_spec_invalid": "định danh model dự phòng không hợp lệ (cần dạng `provider:model` hoặc `p1:m1, p2:m2`)",
        "fb_entry_invalid": "mục model dự phòng {raw!r} không hợp lệ (cần dạng `provider:model`)",
        "fb_moa_forbidden": "cố vấn dự phòng không được phép dùng lại provider `moa`",
        "fb_armed": "Đã bật cố vấn dự phòng → {models} sau {attempts} lần lỗi.",
        "quorum_show": "ngưỡng cố vấn (quorum) true-moa: {label}",
        "quorum_all_set": "Đã đặt ngưỡng cố vấn về `all` (bắt buộc mọi cố vấn đang bật đều phải thành công).",
        "quorum_usage": "cú pháp: /true-moa quorum [all | <1..8>]",
        "quorum_range": "số cố vấn tối thiểu phải từ 1 đến 8 (hoặc `all`)",
        "quorum_set": "Đã đặt ngưỡng cố vấn: yêu cầu tối thiểu {count} cố vấn phản hồi khỏe sau khi thử lại.",
        "progress_retry": "Đang chờ {delay:g}s để thử lại {models} (lần {attempt}/{total})",
        "progress_switched": "Đã chuyển cố vấn lỗi sang mô hình dự phòng {secondary}",
        "outage_ladder_exhausted": " (đã hết thang thử True MoA={ladder})",
        "outage_budget_reached": " (đã chạm trần thời gian chờ True MoA)",
        "outage_message": (
            "Gián đoạn cố vấn MoA: {names} thất bại sau {attempts} lần thử trong {waited:.0f}s{ladder_desc}. "
            "Đã dừng lượt an toàn trước khi mô hình chính tự trả lời một mình; không lưu kết quả lỗi vào bộ nhớ đệm."
        ),
        "doctor_title": "True MoA Doctor — Chẩn đoán sức khỏe cấu hình (Offline)",
        "doctor_verdict_healthy": "Kết luận: KHỎE MẠNH (HEALTHY)",
        "doctor_verdict_warnings": "Kết luận: KHỎE MẠNH (có cảnh báo)",
        "doctor_verdict_issues": "Kết luận: PHÁT HIỆN VẤN ĐỀ ({count})",
        "doctor_gate_ok": "v{plugin_version} trên Hermes {core_version} ({seam_desc})",
        "doctor_gate_fail": "chưa kích hoạt — {error}",
        "doctor_pm_ok": "thuần công cụ pyproject (installs_packages=False, thư mục plugin sạch)",
        "doctor_pm_fail": "phát hiện nguy cơ xung đột môi trường PM ({detail})",
        "doctor_data_ok": "{path}",
        "doctor_data_fail": "dữ liệu lưu trữ không hợp lệ tại {path}",
        "doctor_preset_none": "chưa cấu hình moa.presets trong config.yaml",
        "doctor_preset_zero": "preset '{preset}' chưa bật cố vấn nào",
        "doctor_preset_warn": "preset '{preset}' ({count} cố vấn): {problems}",
        "doctor_preset_ok": "preset '{preset}' ({count} cố vấn đang bật, khóa API hợp lệ)",
        "doctor_fb_empty": "đang bật nhưng chưa nhập hoặc sai cú pháp model dự phòng",
        "doctor_fb_collision": "model dự phòng bị trùng với Aggregator hoặc dùng 'moa'",
        "doctor_fb_warn": "{count} model dự phòng: {problems}",
        "doctor_fb_ok": "{labels} (chuyển sau {attempts} lần lỗi)",
        "doctor_fb_off": "đang tắt (chỉ dùng các cố vấn chính)",
        "doctor_ladder_warn": "trần chờ {budget:g}s nhỏ hơn cả bước nghỉ đầu tiên {first:g}s",
        "doctor_ladder_hb": " (nhịp tim chống treo lượt đang bật)",
        "doctor_ladder_ok": "{attempts} lần gọi, tổng thời gian nghỉ tối đa {total_sleep:g}s{hb_note}",
        "doctor_quorum_all": "bắt buộc tất cả cố vấn đang bật đều thành công",
        "doctor_quorum_min": "yêu cầu tối thiểu {count} cố vấn phản hồi khỏe",
    },
}


def active_language(config: dict[str, Any] | None = None) -> str:
    cfg = config if isinstance(config, dict) else state.load()
    return normalize_language(cfg.get("language"))


def msg(key: str, *, lang: str | None = None, **kwargs: Any) -> str:
    target = normalize_language(lang) if lang is not None else active_language()
    template = MESSAGES.get(target, {}).get(key) or MESSAGES[DEFAULT_LANGUAGE].get(key) or key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template
