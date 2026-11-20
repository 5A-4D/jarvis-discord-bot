from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


MAX_EVENTS_PER_REQUEST = 40
LOGGER = logging.getLogger(__name__)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD|PWD))=([^\s]+)"
)
SECRET_FLAG = re.compile(
    r"(?i)(--?(?:token|api[-_]?key|secret|password|passwd|pwd)(?:=|\s+))([^\s]+)"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_command(command: str) -> str:
    """Keep commands useful to an operator without persisting obvious secrets."""
    command = SECRET_ASSIGNMENT.sub(r"\1=<redacted>", command)
    command = SECRET_FLAG.sub(r"\1<redacted>", command)
    if len(command) > 500:
        command = command[:497] + "..."
    return command


def _checklist_counts(items: object) -> dict[str, int] | None:
    """Extract only counts; task descriptions never enter progress snapshots."""
    if not isinstance(items, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("completed"), bool)
        for item in items
    ):
        return None
    return {
        "completed": sum(item["completed"] for item in items),
        "total": len(items),
    }


def _checklist_summary(counts: object) -> str:
    if isinstance(counts, dict):
        completed, total = counts.get("completed"), counts.get("total")
        if (
            type(completed) is int
            and type(total) is int
            and 0 <= completed <= total
        ):
            if total == 0:
                return "Task checklist is empty"
            return f"Task checklist: {completed}/{total} complete"
    return "Task checklist: progress unavailable"


def describe_codex_event(event: dict[str, Any]) -> tuple[str, str | None] | None:
    """Return a safe operator-facing summary without prompts, replies, or tool output."""
    event_type = event.get("type")
    if event_type == "thread.started":
        return "Codex session started", str(event.get("thread_id") or "") or None
    if event_type == "turn.started":
        return "Codex turn started", None
    if event_type in {"error", "turn.failed"}:
        return "Codex reported an error", None
    if event_type == "turn.completed":
        usage = event.get("usage")
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                return (
                    "Codex turn completed",
                    f"{input_tokens + output_tokens:,} tokens",
                )
        return "Codex turn completed", None
    if event_type not in {"item.started", "item.updated", "item.completed"}:
        return None

    item = event.get("item")
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "work item").replace("_", " ")
    if event_type == "item.updated" and item_type != "todo list":
        return None
    completed = event_type == "item.completed"
    failed = completed and item.get("status") == "failed"
    if item_type == "todo list":
        if failed:
            return "Task checklist update failed", None
        # The checklist item closes when the turn ends, even if some tasks
        # remain unchecked. Its lifecycle is not evidence of task completion.
        return _checklist_summary(_checklist_counts(item.get("items"))), None
    if item_type == "agent message":
        if failed:
            return "Response drafting failed", None
        return ("Response drafted", None) if completed else None
    if item_type == "command execution":
        command = item.get("command")
        detail = _redact_command(command) if isinstance(command, str) else None
        if completed:
            exit_code = item.get("exit_code")
            has_exit_code = isinstance(exit_code, int) and not isinstance(exit_code, bool)
            # item.completed closes an item; a nonzero exit still means the
            # command failed, even when its lifecycle status is "completed".
            failed = failed or (has_exit_code and exit_code != 0)
            summary = "Shell command failed" if failed else "Shell command completed"
            if has_exit_code:
                summary += f" (exit {exit_code})"
            return summary, detail
        return "Running shell command", detail
    if item_type in {"mcp tool call", "tool call"}:
        tool = item.get("tool") or item.get("name")
        detail = str(tool) if isinstance(tool, str) else None
        if failed:
            return "Tool call failed", detail
        return ("Tool call completed" if completed else "Calling tool"), detail
    if item_type in {"web search", "web search call"}:
        if failed:
            return "Web search failed", None
        return ("Web search completed" if completed else "Searching the web"), None
    if failed:
        return f"{item_type.title()} failed", None
    if item_type in {"reasoning", "analysis"}:
        text = item.get("text")
        detail = text.strip() if isinstance(text, str) and text.strip() else None
        if detail and len(detail) > 1_500:
            detail = detail[:1_497] + "..."
        return (
            "Reasoning summary" if completed else "Reasoning",
            detail,
        )
    return (f"{item_type.title()} completed" if completed else f"Starting {item_type}"), None


class LiveProgressStore:
    """Atomic, redacted snapshots of active Codex turns for local operators."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._active: dict[str, dict[str, Any]] = {}
        self._last_completed: dict[str, Any] | None = None
        self._interrupted: list[dict[str, Any]] = []
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, listener: Callable[[], None]) -> None:
        """Notify an in-process viewer whenever the published snapshot changes."""
        self._listeners.append(listener)

    def _notify_listeners(self) -> None:
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:
                LOGGER.exception("Live progress listener failed")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generated_at": _now(),
            "active": list(self._active.values()),
            "last_completed": self._last_completed,
            "interrupted": self._interrupted,
        }

    async def snapshot(self) -> dict[str, Any]:
        """Return a stable copy suitable for an asynchronous live viewer."""
        async with self._lock:
            return copy.deepcopy(self._payload())

    async def reset(self) -> None:
        """Recover the previous snapshot without claiming old work is still running."""
        async with self._lock:
            previous = await asyncio.to_thread(_read_payload, self.path)
            if previous.get("read_error"):
                LOGGER.warning(
                    "Could not recover previous live progress: %s", previous["read_error"]
                )
            timestamp = _now()
            interrupted = []
            for request in previous.get("active", []):
                recovered = copy.deepcopy(request)
                recovered["outcome"] = "interrupted"
                # Restart time is when the interruption was discovered, not
                # when the work stopped. Keep the last observed activity so
                # viewers do not count offline time as execution time.
                recovered["last_activity_at"] = request.get("updated_at")
                recovered["interrupted_at"] = timestamp
                recovered["updated_at"] = timestamp
                recovered.pop("completed_at", None)
                events = recovered.get("events", [])
                events.append({
                    "at": timestamp,
                    "summary": "Request interrupted before restart; execution was not resumed",
                })
                recovered["events"] = events[-MAX_EVENTS_PER_REQUEST:]
                interrupted.append(recovered)
            self._active.clear()
            self._interrupted = interrupted or previous.get("interrupted", [])
            self._last_completed = (
                interrupted[-1] if interrupted else previous.get("last_completed")
            )
            await self._publish()

    async def start(
        self,
        request_id: str,
        *,
        mode: str,
        conversation_id: str,
        session_id: str | None,
    ) -> None:
        timestamp = _now()
        async with self._lock:
            previous = self._active.get(request_id)
            self._active[request_id] = {
                "request_id": request_id,
                "mode": mode,
                "conversation_id": conversation_id,
                "session_id": session_id,
                "started_at": timestamp,
                "updated_at": timestamp,
                "events": [
                    {"at": timestamp, "summary": "Discord request accepted"}
                ],
            }
            try:
                # If registration is cancelled, publish its final cleanup state
                # before waking viewers rather than advertising abandoned work.
                await self._publish(notify_on_cancellation=False)
            except asyncio.CancelledError:
                # start() has not returned, so the caller has not yet enabled
                # its finish() cleanup. We must close this request ourselves,
                # after the initial writer has stopped and while holding the
                # same lock. A cancelled replacement must preserve the original.
                if previous is None:
                    request = self._active.pop(request_id)
                    completed_at = _now()
                    request["outcome"] = "cancelled"
                    request["completed_at"] = completed_at
                    request["updated_at"] = completed_at
                    request["events"].append({
                        "at": completed_at,
                        "summary": "Request cancelled before execution started",
                    })
                    self._last_completed = request
                else:
                    self._active[request_id] = previous
                try:
                    await self._publish()
                except asyncio.CancelledError:
                    # _publish already waited for the cleanup writer. Keep the
                    # original cancellation even if more arrived in the meantime.
                    pass
                except OSError:
                    LOGGER.exception("Could not persist cancelled live progress registration")
                raise
            except OSError:
                # The caller only arranges finish() after start() succeeds. A
                # rejected atomic write must not leave an unfinishable request
                # that a later event or another request's write can publish.
                if previous is None:
                    self._active.pop(request_id, None)
                else:
                    self._active[request_id] = previous
                raise

    async def record(self, request_id: str, event: dict[str, Any]) -> None:
        described = describe_codex_event(event)
        if described is None:
            return
        timestamp = _now()
        summary, detail = described
        async with self._lock:
            request = self._active.get(request_id)
            if request is None:
                return
            if event.get("type") == "thread.started" and event.get("thread_id"):
                request["session_id"] = str(event["thread_id"])
            item = event.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") in ("todo_list", "todo list")
                and not (
                    event.get("type") == "item.completed"
                    and item.get("status") == "failed"
                )
            ):
                # Keep the current counts independently of the bounded event
                # history. An unreadable update invalidates stale counts.
                request["checklist"] = _checklist_counts(item.get("items"))
            progress_event: dict[str, Any] = {"at": timestamp, "summary": summary}
            if detail:
                progress_event["detail"] = detail
            request["events"].append(progress_event)
            request["events"] = request["events"][-MAX_EVENTS_PER_REQUEST:]
            request["updated_at"] = timestamp
            await self._publish()

    async def finish(self, request_id: str, outcome: str) -> None:
        timestamp = _now()
        # Completion is cleanup even before it owns the lock. Shield the lock
        # waiter so cancellation cannot abandon a terminal request behind an
        # in-flight update, or move it to the back of the queue on every cancel.
        acquiring = asyncio.create_task(self._lock.acquire())
        cancellation: asyncio.CancelledError | None = None
        while not acquiring.done():
            try:
                await asyncio.shield(acquiring)
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = error
        acquiring.result()
        try:
            request = self._active.pop(request_id, None)
            if request is not None:
                request["outcome"] = outcome
                request["completed_at"] = timestamp
                request["updated_at"] = timestamp
                self._last_completed = request
            try:
                await self._publish()
            except OSError:
                # The caller logs a failed finish and moves on. Retry once so
                # a transient disk failure does not leave completed work active
                # on disk indefinitely. Keep the same outcome and timestamps,
                # and hold the lock across both writers. Persistent failures
                # still reach the caller; cancellation is never retried here.
                LOGGER.debug("Retrying final live progress snapshot after write failure")
                await self._publish()
        except asyncio.CancelledError as error:
            # _publish has already waited for its writer. Preserve the first
            # cancellation if another arrived after the lock was acquired.
            if cancellation is None:
                cancellation = error
        except Exception:
            if cancellation is None:
                raise
            LOGGER.exception("Could not persist cancelled live progress completion")
        finally:
            self._lock.release()
        if cancellation is not None:
            raise cancellation

    async def _publish(self, *, notify_on_cancellation: bool = True) -> None:
        """Keep the caller's lock until its worker finishes, even on cancellation."""
        writer = asyncio.create_task(asyncio.to_thread(self._write))
        cancellation: asyncio.CancelledError | None = None
        # Cancelling to_thread's await does not stop the underlying thread. A
        # later finish/reset must not mutate its payload or share its temp file.
        while not writer.done():
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = error
            except Exception:
                break  # Retrieve and handle the worker's exception below.
        try:
            writer.result()
        except Exception:
            if cancellation is None:
                raise
            # Preserve the request's cancellation, but observe the disk failure
            # rather than losing an exception in a detached background task.
            LOGGER.exception("Could not persist interrupted live progress update")
        else:
            if cancellation is None or notify_on_cancellation:
                self._notify_listeners()
        if cancellation is not None:
            raise cancellation

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        payload = self._payload()
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _duration(started_at: object, ended_at: object | None = None) -> str:
    start = _parse_time(started_at)
    end = _parse_time(ended_at) if ended_at else datetime.now(timezone.utc)
    if start is None or end is None:
        return "unknown"
    seconds = max(0, int((end - start).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _request_duration(request: dict[str, Any]) -> str:
    if request.get("outcome") == "interrupted":
        last_activity = request.get("last_activity_at")
        if not last_activity:
            return "unknown"
        duration = _duration(request.get("started_at"), last_activity)
        return f"at least {duration}" if duration != "unknown" else "unknown"
    return _duration(
        request.get("started_at"),
        request.get("completed_at") if request.get("outcome") else None,
    )


def render_progress(payload: dict[str, Any], *, verbose: bool = False) -> str:
    active = payload.get("active")
    active_requests = active if isinstance(active, list) else []
    lines = [f"Jarvis Codex progress — {len(active_requests)} active request(s)"]
    if payload.get("read_error"):
        lines[0] = "Jarvis Codex progress — active request count unknown"
        lines.append(f"Progress state unavailable: {payload['read_error']}")
        return "\n".join(lines)
    last = payload.get("last_completed")
    if isinstance(last, dict):
        outcome = str(last.get("outcome") or "unknown")
        elapsed = _request_duration(last)
        last_result = f"Last: {last.get('request_id', '?')} ({outcome}, {elapsed})."
        if active_requests:
            # Discord retains this heading when it truncates long histories.
            # A concurrent request must not hide the latest terminal outcome.
            lines[0] += f" · {last_result}"
    interrupted = payload.get("interrupted")
    if isinstance(interrupted, list) and interrupted:
        identifiers = ", ".join(
            str(request.get("request_id", "?")) for request in interrupted
        )
        lines.append(
            f"Interrupted before restart: {identifiers}. Execution was not resumed."
        )
    requests = active_requests
    if not requests:
        if not isinstance(last, dict):
            lines.append("No active request and no completed request recorded since startup.")
            return "\n".join(lines)
        lines.append(f"No active request. {last_result}")
        requests = [last]

    for request in requests:
        request_id = request.get("request_id", "?")
        elapsed = _request_duration(request)
        lines.append("")
        lines.append(
            f"request {request_id} · {request.get('mode', 'unknown')} · {elapsed}"
        )
        session_id = request.get("session_id")
        if session_id:
            lines.append(f"session {session_id}")
        events = request.get("events")
        if not isinstance(events, list):
            events = []
        for event in events:
            if not isinstance(event, dict):
                continue
            at = _parse_time(event.get("at"))
            clock = at.astimezone().strftime("%H:%M:%S") if at else "??:??:??"
            line = f"[{clock}] {event.get('summary', 'Progress update')}"
            if verbose and event.get("detail"):
                line += f" — {event['detail']}"
            lines.append(line)
        if "checklist" in request:
            # Keep the current counts at the tail, where Discord retains them
            # when it omits a long history of earlier activity.
            lines.append(_checklist_summary(request["checklist"]))
    return "\n".join(lines)


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"active": [], "last_completed": None}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {"active": [], "last_completed": None, "read_error": str(error)}
    if not isinstance(payload, dict):
        return {"active": [], "read_error": "Progress snapshot is not an object"}
    # A damaged entry must not erase unrelated requests during restart.
    # Keep a warning so read-only viewers still report an unknown active count
    # until reset() publishes the recovered state. Never include rejected data
    # in that warning: it may contain private request content.
    errors = []

    def recoverable(request: object) -> bool:
        return isinstance(request, dict) and isinstance(request.get("events", []), list)

    for field in ("active", "interrupted"):
        entries = payload.get(field, [])
        if not isinstance(entries, list):
            errors.append(f"Progress {field} is not a list")
            payload[field] = []
            continue
        valid = [request for request in entries if recoverable(request)]
        rejected = len(entries) - len(valid)
        if rejected:
            errors.append(f"Skipped {rejected} invalid progress {field} request(s)")
        payload[field] = valid
    last = payload.get("last_completed")
    if last is not None and not recoverable(last):
        errors.append("Invalid progress last_completed request")
        payload["last_completed"] = None
    if errors:
        payload["read_error"] = "; ".join(errors)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect live, redacted progress for Discord-triggered Codex turns."
    )
    parser.add_argument(
        "--path", type=Path, default=Path("data/live-progress.json")
    )
    parser.add_argument("-f", "--follow", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--interval", type=float, default=1.0)
    arguments = parser.parse_args()

    last_rendered: str | None = None
    try:
        while True:
            rendered = render_progress(
                _read_payload(arguments.path), verbose=arguments.verbose
            )
            if rendered != last_rendered:
                if last_rendered is not None:
                    print("\n" + "─" * 72)
                print(rendered, flush=True)
                last_rendered = rendered
            if not arguments.follow:
                break
            time.sleep(max(0.1, arguments.interval))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
