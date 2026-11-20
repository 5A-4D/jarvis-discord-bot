from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RequestTelemetry:
    request_id: str
    parent_request_id: str | None
    chain_id: str | None
    conversation_id: str
    session_id: str | None
    mode: str
    provider: str
    model: str | None
    model_source: str
    started_at: str
    completed_at: str
    latency_ms: int
    outcome: str
    finish_reason: str | None
    error_type: str | None
    usage_source: str
    input_tokens: int | None
    cached_input_tokens: int | None
    reasoning_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    estimated_cost: float | None = None
    currency: str | None = None
    pricing_version: str | None = None
    schema_version: int = 1


class JSONLTelemetryStore:
    """Append-only request telemetry with process-local write serialization."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def record(self, event: RequestTelemetry) -> None:
        """Finish terminal telemetry cleanup before propagating cancellation."""
        line = json.dumps(asdict(event), separators=(",", ":"), sort_keys=True)
        # Shield both the lock wait and the append. Cancelling to_thread's
        # await cannot stop its worker; releasing the lock early permits a
        # following record to interleave with the unfinished JSONL write.
        writer = asyncio.create_task(self._record_line(line))
        cancellation: asyncio.CancelledError | None = None
        while not writer.done():
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = error
            except Exception:
                break  # Observe the writer's failure below, including on cancellation.
        try:
            writer.result()
        except Exception:
            if cancellation is None:
                raise
            # Do not lose a disk failure in a detached worker, or mask the
            # caller's original interruption with that later failure.
            LOGGER.exception("Could not persist cancelled request telemetry")
        if cancellation is not None:
            raise cancellation

    async def _record_line(self, line: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.write("\n")


def read_records(path: Path) -> Iterable[dict[str, object]]:
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL on line {line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"Telemetry line {line_number} is not an object")
            yield record


def aggregate_records(
    records: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    groups: defaultdict[tuple[str, str, str], dict[str, int | float]] = defaultdict(
        lambda: {
            "requests": 0,
            "successes": 0,
            "errors": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "latency_ms": 0,
            "cost": 0.0,
            "cost_records": 0,
        }
    )
    for record in records:
        started_at = str(record.get("started_at") or "unknown")
        date = started_at[:10] if len(started_at) >= 10 else "unknown"
        mode = str(record.get("mode") or "unknown")
        model = str(record.get("model") or "unknown")
        group = groups[(date, mode, model)]
        group["requests"] += 1
        outcome = str(record.get("outcome") or "unknown")
        if outcome == "success":
            group["successes"] += 1
        else:
            group["errors"] += 1
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
            "output_tokens",
            "total_tokens",
            "latency_ms",
        ):
            value = record.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                group[field] += value
        cost = record.get("estimated_cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            group["cost"] += float(cost)
            group["cost_records"] += 1

    summaries: list[dict[str, object]] = []
    for (date, mode, model), values in sorted(groups.items()):
        requests = int(values["requests"])
        input_tokens = int(values["input_tokens"])
        cached_tokens = int(values["cached_input_tokens"])
        summaries.append(
            {
                "date": date,
                "mode": mode,
                "model": model,
                "requests": requests,
                "successes": int(values["successes"]),
                "errors": int(values["errors"]),
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_tokens,
                "cache_rate": cached_tokens / input_tokens if input_tokens else None,
                "reasoning_tokens": int(values["reasoning_tokens"]),
                "output_tokens": int(values["output_tokens"]),
                "total_tokens": int(values["total_tokens"]),
                "average_latency_ms": round(float(values["latency_ms"]) / requests),
                "estimated_cost": (
                    round(float(values["cost"]), 8)
                    if values["cost_records"]
                    else None
                ),
            }
        )
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate Jarvis request telemetry")
    parser.add_argument("path", type=Path, help="JSONL telemetry file")
    arguments = parser.parse_args()
    for summary in aggregate_records(read_records(arguments.path)):
        print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
