"""Pure-Python optimization engines for True MoA (stdlib only, zero Hermes imports).

Contains bounded, thread-safe trackers for per-reference prefix-cache diagnosis,
event-driven adaptive fan-out, chunked prefix-preserving context trimming,
concise static system prompting, empty/refusal output detection, and sticky
secondary failover pins.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict, deque
from typing import Any

CONCISE_ADVISORY_MARKER = "[True MoA Concise Advisory Mode]"
CONCISE_ADVISORY_ADDENDUM = (
    f"\n\n{CONCISE_ADVISORY_MARKER}\n"
    "Be concise and high-signal. Skip preamble, restating the prompt, or meta disclaimers. "
    "Structure your advice under three brief headings:\n"
    "1. Core diagnosis\n"
    "2. Concrete next steps / tool strategy\n"
    "3. Key risks or blind spots"
)

_EMPTY_OR_REFUSAL_MAX_CHARS = 220
_REFUSAL_PATTERNS = (
    re.compile(r"\bi\s+(?:cannot|can't|do\s+not|don't|am\s+unable\s+to)\s+(?:access|read|open|view|browse|run|execute|inspect|check)\b", re.IGNORECASE),
    re.compile(r"\bunable\s+to\s+(?:access|read|open|view|run|execute|inspect)\b", re.IGNORECASE),
    re.compile(r"\bi\s+don'?t\s+have\s+(?:access|tools|the\s+ability)\s+to\b", re.IGNORECASE),
    re.compile(r"\bas\s+an\s+ai(?:\s+(?:language\s+)?model)?\b", re.IGNORECASE),
)
_ACTIONABLE_STRUCTURE_RE = re.compile(r"(?:`[^`]+`|(?:^|\n)\s*(?:[-*]|\d+[.)])\s+\S)", re.MULTILINE)

_TOOL_ERROR_SIGNAL_RE = re.compile(
    r"(?:"
    r"(?:exited?\s+with\s+code|exit[_\s]?code\s*[:=]|returncode\s*[:=])\s*[1-9]\d*"
    r"|traceback\s+\(most\s+recent\s+call\s+last\)"
    r"|(?:^|\n)\s*(?:E\s+)?(?:AssertionError|SyntaxError|TypeError|ValueError|ImportError|ModuleNotFoundError|KeyError|AttributeError|FileNotFoundError|RuntimeError|ReferenceError)\b(?::|\s*\()"
    r"|(?:^|\n)\s*(?:fatal:|command\s+failed\b|permission\s+denied\b|connection\s+timed\s+out\b|FAILED(?:\s+\(|\s+\S|\b))"
    r"|\b\d+\s+failed\b"
    r"|\bhttp(?:\s+status|\s+error|\s+code)?\s*(?:429|500|502|503|504)\b"
    r"|\berror\s+code:\s*(?:429|500|502|503|504)\b"
    r"|\btimed\s+out\b"
    r")",
    re.IGNORECASE,
)


def _canonical_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type", "text") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, dict):
                cleaned = {k: v for k, v in item.items() if k != "cache_control"}
                parts.append(json.dumps(cleaned, sort_keys=True, ensure_ascii=False, default=str))
            else:
                parts.append(str(item))
        return "\u0001".join(parts)
    try:
        return json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(content)


def _message_digest_pair(msg: dict[str, Any]) -> tuple[str, int]:
    role = str(msg.get("role") or "")
    text = _canonical_content_text(msg.get("content"))
    digest = hashlib.sha256(f"{role}\u0000{text}".encode("utf-8", "replace")).hexdigest()
    return digest, len(text)


def message_signatures(messages: list[dict[str, Any]]) -> tuple[tuple[str, int], ...]:
    return tuple(_message_digest_pair(m) for m in messages if isinstance(m, dict))


def hash_messages(messages: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for m in messages:
        if not isinstance(m, dict):
            continue
        digest, _length = _message_digest_pair(m)
        h.update(digest.encode("ascii"))
        h.update(b"\x00")
    return h.hexdigest()


class PrefixTracker:
    """Privacy-safe per-reference prefix-hash tracker and cache-miss diagnostician."""

    def __init__(self, max_entries: int = 128) -> None:
        self._max_entries = max(8, int(max_entries))
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, int], tuple[tuple[tuple[str, int], ...], float, str]] = OrderedDict()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def observe(
        self,
        *,
        preset: str,
        label: str,
        model_key: str,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        latency_ms: int,
        slot_index: int = 0,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = time.monotonic() if now is None else float(now)
        sigs = message_signatures(messages)
        key = (str(preset or ""), int(slot_index))
        norm_model = str(model_key or label or "")

        input_tokens = max(0, int(usage.get("input_tokens") or 0))
        cache_read_tokens = max(0, int(usage.get("cache_read_tokens") or 0))
        cache_write_tokens = max(0, int(usage.get("cache_write_tokens") or 0))
        output_tokens = max(0, int(usage.get("output_tokens") or 0))
        reasoning_tokens = max(0, int(usage.get("reasoning_tokens") or 0))
        prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
        cache_hit_pct = round(100.0 * cache_read_tokens / prompt_tokens, 1) if prompt_tokens > 0 else 0.0

        with self._lock:
            prev = self._entries.pop(key, None)
            self._entries[key] = (sigs, ts, norm_model)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

        if prev is None:
            prefix_match_pct = 0.0
            gap_s = 0.0
            diagnosis = "hit" if cache_hit_pct >= 50.0 else "first_call"
        else:
            prev_sigs, prev_ts, prev_model = prev
            gap_s = round(max(0.0, ts - prev_ts), 1)
            total_prev_chars = sum(length for _h, length in prev_sigs)
            if total_prev_chars == 0:
                prefix_match_pct = 100.0 if sigs == prev_sigs else 0.0
            else:
                matched_chars = 0
                for p_item, c_item in zip(prev_sigs, sigs, strict=False):
                    if p_item != c_item:
                        break
                    matched_chars += p_item[1]
                prefix_match_pct = round(100.0 * matched_chars / total_prev_chars, 1)

            if cache_hit_pct >= 50.0:
                diagnosis = "hit"
            elif 10.0 <= cache_hit_pct < 50.0:
                diagnosis = "partial_hit"
            elif prompt_tokens < 1024:
                diagnosis = "too_short"
            elif prev_model != norm_model:
                diagnosis = "miss_model_switched"
            elif prefix_match_pct < 50.0:
                diagnosis = "miss_prefix_shifted"
            elif prefix_match_pct >= 80.0 and gap_s >= 300.0:
                diagnosis = "miss_ttl_expired"
            elif prefix_match_pct >= 80.0 and gap_s < 300.0:
                diagnosis = "miss_provider_relay"
            else:
                diagnosis = "miss_other"

        return {
            "preset": key[0],
            "slot_index": key[1],
            "label": str(label or ""),
            "model_key": norm_model,
            "prompt_tokens": prompt_tokens,
            "input_tokens": input_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cache_hit_pct": cache_hit_pct,
            "prefix_match_pct": prefix_match_pct,
            "gap_s": gap_s,
            "latency_ms": max(0, int(latency_ms)),
            "diagnosis": diagnosis,
        }


def _extract_tool_result_segments(messages: list[dict[str, Any]]) -> list[str]:
    segments: list[str] = []
    marker = "[tool result:"
    call_marker = "\n[called tool:"
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        text = _canonical_content_text(msg.get("content"))
        if role == "tool" and text:
            segments.append(text)
            continue
        pos = 0
        while True:
            idx = text.find(marker, pos)
            if idx < 0:
                break
            body_start = idx + len(marker)
            next_tool = text.find(marker, body_start)
            next_call = text.find(call_marker, body_start)
            stops = [s for s in (next_tool, next_call) if s >= 0]
            end = min(stops) if stops else len(text)
            segments.append(text[body_start:end])
            pos = end
    return segments


def has_tool_error_signal(messages: list[dict[str, Any]]) -> bool:
    """Return True when new tool-result blocks show a concrete failure or traceback."""
    for seg in _extract_tool_result_segments(messages):
        if _TOOL_ERROR_SIGNAL_RE.search(seg):
            return True
    return False


class _AdaptiveState:
    __slots__ = (
        "full_sig",
        "last_msg_chars",
        "last_msg_hash",
        "last_refresh_ts",
        "last_seen_full_sig",
        "last_seen_slots_sig",
        "msg_count",
        "slots_sig",
        "steps_since_refresh",
        "total_chars",
        "turn_sig",
    )

    def __init__(
        self,
        *,
        turn_sig: str,
        full_sig: str,
        slots_sig: tuple[str, ...],
        msg_count: int,
        total_chars: int,
        last_msg_chars: int,
        last_msg_hash: str,
        last_refresh_ts: float,
        steps_since_refresh: int = 0,
    ) -> None:
        self.turn_sig = turn_sig
        self.full_sig = full_sig
        self.slots_sig = slots_sig
        self.last_seen_full_sig = full_sig
        self.last_seen_slots_sig = slots_sig
        self.msg_count = msg_count
        self.total_chars = total_chars
        self.last_msg_chars = last_msg_chars
        self.last_msg_hash = last_msg_hash
        self.last_refresh_ts = last_refresh_ts
        self.steps_since_refresh = steps_since_refresh


class AdaptiveFanoutTracker:
    """Decide per-iteration whether to refresh advisors or reuse cached turn guidance."""

    def __init__(self, max_facades: int = 64) -> None:
        self._max_facades = max(8, int(max_facades))
        self._lock = threading.Lock()
        self._states: OrderedDict[int, _AdaptiveState] = OrderedDict()

    def clear(self) -> None:
        with self._lock:
            self._states.clear()

    def evaluate(
        self,
        *,
        facade_id: int,
        ref_messages: list[dict[str, Any]],
        slots_sig: tuple[str, ...],
        advisory_instruction: str,
        has_cached_outputs: bool,
        max_interval: int,
        keepalive_s: int,
        delta_chars_threshold: int = 16000,
        now: float | None = None,
    ) -> tuple[str, str, dict[str, Any]]:
        ts = time.monotonic() if now is None else float(now)
        body_msgs = ref_messages
        if (
            advisory_instruction
            and ref_messages
            and isinstance(ref_messages[-1], dict)
            and ref_messages[-1].get("role") == "user"
            and ref_messages[-1].get("content") == advisory_instruction
        ):
            body_msgs = ref_messages[:-1]

        last_user = next(
            (
                idx
                for idx in range(len(body_msgs) - 1, -1, -1)
                if isinstance(body_msgs[idx], dict)
                and body_msgs[idx].get("role") == "user"
                and body_msgs[idx].get("content") != advisory_instruction
            ),
            None,
        )
        turn_prefix = body_msgs[: last_user + 1] if last_user is not None else body_msgs
        turn_sig = hash_messages(turn_prefix)
        full_sig = hash_messages(ref_messages)
        sigs = message_signatures(body_msgs)
        msg_count = len(body_msgs)
        total_chars = sum(length for _h, length in sigs)
        last_msg_hash = sigs[-1][0] if sigs else ""
        last_msg_chars = sigs[-1][1] if sigs else 0
        snapshot: dict[str, Any] = {
            "turn_sig": turn_sig,
            "full_sig": full_sig,
            "slots_sig": tuple(slots_sig),
            "msg_count": msg_count,
            "total_chars": total_chars,
            "last_msg_chars": last_msg_chars,
            "last_msg_hash": last_msg_hash,
            "ts": ts,
        }

        with self._lock:
            prev = self._states.get(facade_id)
            if not has_cached_outputs or prev is None:
                return "refresh", "no_cache", snapshot
            self._states.move_to_end(facade_id)

            if (
                (full_sig == prev.last_seen_full_sig and snapshot["slots_sig"] == prev.last_seen_slots_sig)
                or (full_sig == prev.full_sig and snapshot["slots_sig"] == prev.slots_sig)
            ):
                return "reuse", "identical_retry", snapshot
            if turn_sig != prev.turn_sig:
                return "refresh", "new_turn", snapshot
            if snapshot["slots_sig"] != prev.slots_sig:
                return "refresh", "slots_changed", snapshot
            if msg_count < prev.msg_count:
                return "refresh", "context_shrunk", snapshot

            delta_msgs: list[dict[str, Any]] = []
            if prev.msg_count > 0 and msg_count >= prev.msg_count:
                anchor_msg = body_msgs[prev.msg_count - 1]
                if isinstance(anchor_msg, dict):
                    tail_text = _canonical_content_text(anchor_msg.get("content"))
                    cur_hash, cur_chars = _message_digest_pair(anchor_msg)
                    if cur_chars > prev.last_msg_chars:
                        delta_msgs.append({"role": anchor_msg.get("role"), "content": tail_text[prev.last_msg_chars :]})
                    elif cur_hash != prev.last_msg_hash:
                        delta_msgs.append(anchor_msg)
            if msg_count > prev.msg_count:
                delta_msgs.extend(m for m in body_msgs[prev.msg_count :] if isinstance(m, dict))
            if delta_msgs and has_tool_error_signal(delta_msgs):
                return "refresh", "tool_error", snapshot

            if total_chars - prev.total_chars >= max(1000, int(delta_chars_threshold)):
                return "refresh", "large_delta", snapshot
            if keepalive_s > 0 and (ts - prev.last_refresh_ts) >= float(keepalive_s):
                return "refresh", "ttl_keepalive", snapshot

            next_step = prev.steps_since_refresh + 1
            if next_step >= max(2, int(max_interval)):
                return "refresh", "max_interval", snapshot

            prev.last_seen_full_sig = full_sig
            prev.last_seen_slots_sig = snapshot["slots_sig"]
            prev.steps_since_refresh = next_step
            return "reuse", "mechanical_step", snapshot

    def mark_refreshed(self, facade_id: int, snapshot: dict[str, Any]) -> None:
        state_obj = _AdaptiveState(
            turn_sig=str(snapshot["turn_sig"]),
            full_sig=str(snapshot["full_sig"]),
            slots_sig=tuple(snapshot["slots_sig"]),
            msg_count=int(snapshot["msg_count"]),
            total_chars=int(snapshot["total_chars"]),
            last_msg_chars=int(snapshot["last_msg_chars"]),
            last_msg_hash=str(snapshot.get("last_msg_hash") or ""),
            last_refresh_ts=float(snapshot["ts"]),
            steps_since_refresh=0,
        )
        with self._lock:
            self._states.pop(facade_id, None)
            self._states[facade_id] = state_obj
            while len(self._states) > self._max_facades:
                self._states.popitem(last=False)


class ChunkedTrimTracker:
    """Drop overflowing history in chunks and pin the surviving boundary for prefix reuse."""

    def __init__(self, max_slots: int = 64, max_anchors_per_slot: int = 8) -> None:
        self._max_slots = max(8, int(max_slots))
        self._max_anchors = max(2, int(max_anchors_per_slot))
        self._lock = threading.Lock()
        self._pins: OrderedDict[tuple[str, str], deque[tuple[str, str]]] = OrderedDict()

    def clear(self) -> None:
        with self._lock:
            self._pins.clear()

    @staticmethod
    def _anchor_sig(messages: list[dict[str, Any]], idx: int) -> tuple[str, str]:
        first = _message_digest_pair(messages[idx])[0]
        second = _message_digest_pair(messages[idx + 1])[0] if idx + 1 < len(messages) else ""
        return first, second

    def trim(
        self,
        messages: list[dict[str, Any]],
        weights: list[int],
        budget: int,
        *,
        slot_key: tuple[str, str],
        target_ratio: float = 0.75,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        if not messages or budget <= 0 or len(messages) != len(weights):
            return messages, 0, False
        total = sum(weights)
        if total <= budget:
            return messages, 0, False

        has_system = bool(messages) and messages[0].get("role") == "system"
        head_count = 1 if has_system else 0
        head_weight = sum(weights[:head_count])
        n = len(messages)

        with self._lock:
            slot_pins = list(self._pins.get(slot_key) or ())

        if slot_pins and n - head_count > 1:
            pin_set = set(slot_pins)
            running_tail = sum(weights[head_count:])
            for idx in range(head_count, n - 1):
                if idx > head_count:
                    running_tail -= weights[idx - 1]
                if n - idx < 2:
                    break
                if (
                    messages[idx].get("role") == "user"
                    and head_weight + running_tail <= budget
                    and self._anchor_sig(messages, idx) in pin_set
                ):
                    with self._lock:
                        if slot_key in self._pins:
                            self._pins.move_to_end(slot_key)
                    return messages[:head_count] + messages[idx:], idx - head_count, True

        target = max(1, min(budget, int(budget * target_ratio)))
        start = head_count
        remaining = n - head_count
        running_total = total
        while remaining > 2 and running_total > target:
            running_total -= weights[start]
            start += 1
            remaining -= 1
            while remaining > 2 and messages[start].get("role") == "assistant":
                running_total -= weights[start]
                start += 1
                remaining -= 1
        while remaining > 1 and messages[start].get("role") == "assistant":
            running_total -= weights[start]
            start += 1
            remaining -= 1

        if start < n - 1 and messages[start].get("role") == "user":
            anchor = self._anchor_sig(messages, start)
            with self._lock:
                dq = self._pins.pop(slot_key, None)
                if dq is None:
                    dq = deque(maxlen=self._max_anchors)
                if anchor not in dq:
                    dq.append(anchor)
                self._pins[slot_key] = dq
                while len(self._pins) > self._max_slots:
                    self._pins.popitem(last=False)

        trimmed = messages[:head_count] + messages[start:]
        return trimmed, n - len(trimmed), False


def apply_concise_system_prompt(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a shallow copy with static concise instructions appended to system prompt once."""
    if not messages or not isinstance(messages[0], dict):
        return messages
    sys_msg = messages[0]
    if sys_msg.get("role") != "system":
        return messages
    content = sys_msg.get("content")
    if not isinstance(content, str) or CONCISE_ADVISORY_MARKER in content:
        return messages
    updated = dict(sys_msg)
    updated["content"] = content + CONCISE_ADVISORY_ADDENDUM
    return [updated, *messages[1:]]


def is_empty_or_refusal_response(text: Any) -> bool:
    """True for blank responses, empty sentinel, or short tool-access refusals lacking substance."""
    if text is None:
        return True
    if not isinstance(text, str):
        text = str(text)
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.lower() == "(empty response)":
        return True
    if len(stripped) > _EMPTY_OR_REFUSAL_MAX_CHARS:
        return False
    if _ACTIONABLE_STRUCTURE_RE.search(stripped):
        return False
    return any(pattern.search(stripped) for pattern in _REFUSAL_PATTERNS)


class StickyFallbackTracker:
    """Keep a healthy backup reference warm for a bounded TTL after primary failover."""

    def __init__(self, max_entries: int = 64) -> None:
        self._max_entries = max(8, int(max_entries))
        self._lock = threading.Lock()
        self._pins: OrderedDict[tuple[str, str], tuple[dict[str, Any], float]] = OrderedDict()

    def clear(self) -> None:
        with self._lock:
            self._pins.clear()

    def pin(
        self,
        primary_id: tuple[str, str],
        backup_slot: dict[str, Any],
        *,
        ttl_s: float,
        now: float | None = None,
    ) -> None:
        if ttl_s <= 0:
            return
        ts = time.monotonic() if now is None else float(now)
        expiry = ts + float(ttl_s)
        with self._lock:
            self._pins.pop(primary_id, None)
            self._pins[primary_id] = (dict(backup_slot), expiry)
            while len(self._pins) > self._max_entries:
                self._pins.popitem(last=False)

    def get(self, primary_id: tuple[str, str], *, now: float | None = None) -> dict[str, Any] | None:
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            entry = self._pins.get(primary_id)
            if entry is None:
                return None
            slot, expiry = entry
            if ts >= expiry:
                self._pins.pop(primary_id, None)
                return None
            self._pins.move_to_end(primary_id)
            return dict(slot)

    def invalidate(self, primary_id: tuple[str, str]) -> None:
        with self._lock:
            self._pins.pop(primary_id, None)

    def active_count(self, *, now: float | None = None) -> int:
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            expired = [k for k, (_slot, expiry) in self._pins.items() if ts >= expiry]
            for k in expired:
                self._pins.pop(k, None)
            return len(self._pins)


PREFIX_TRACKER = PrefixTracker()
ADAPTIVE_TRACKER = AdaptiveFanoutTracker()
CHUNKED_TRIM_TRACKER = ChunkedTrimTracker()
STICKY_FALLBACK_TRACKER = StickyFallbackTracker()


def reset_trackers() -> None:
    """Reset all in-memory optimization trackers (called on uninstall and test teardown)."""
    PREFIX_TRACKER.clear()
    ADAPTIVE_TRACKER.clear()
    CHUNKED_TRIM_TRACKER.clear()
    STICKY_FALLBACK_TRACKER.clear()
