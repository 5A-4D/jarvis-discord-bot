import tempfile
import unittest
from pathlib import Path

from jarvis_bot.telemetry import (
    JSONLTelemetryStore,
    RequestTelemetry,
    aggregate_records,
    read_records,
)


def sample_event(**overrides: object) -> RequestTelemetry:
    values: dict[str, object] = {
        "request_id": "request-1",
        "parent_request_id": None,
        "chain_id": "chain-1",
        "conversation_id": "conversation-1",
        "session_id": "session-1",
        "mode": "chat",
        "provider": "openai",
        "model": "model-a",
        "model_source": "configured",
        "started_at": "2026-09-10T18:00:00+00:00",
        "completed_at": "2026-09-10T18:00:01+00:00",
        "latency_ms": 1000,
        "outcome": "success",
        "finish_reason": None,
        "error_type": None,
        "usage_source": "provider_reported",
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "reasoning_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 120,
    }
    values.update(overrides)
    return RequestTelemetry(**values)  # type: ignore[arg-type]


class TelemetryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_appends_jsonl_without_prompt_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry.jsonl"
            store = JSONLTelemetryStore(path)

            await store.record(sample_event())

            records = list(read_records(path))
            self.assertEqual(records[0]["request_id"], "request-1")
            self.assertNotIn("prompt", records[0])
            self.assertNotIn("response", records[0])


class TelemetryAggregationTests(unittest.TestCase):
    def test_groups_by_date_mode_and_model(self) -> None:
        records = [
            {
                "started_at": "2026-09-10T18:00:00+00:00",
                "mode": "chat",
                "model": "model-a",
                "outcome": "success",
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "reasoning_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 120,
                "latency_ms": 1000,
                "estimated_cost": None,
            },
            {
                "started_at": "2026-09-10T18:01:00+00:00",
                "mode": "chat",
                "model": "model-a",
                "outcome": "error",
                "input_tokens": 50,
                "cached_input_tokens": 10,
                "reasoning_tokens": 0,
                "output_tokens": 5,
                "total_tokens": 55,
                "latency_ms": 500,
                "estimated_cost": None,
            },
        ]

        summary = aggregate_records(records)[0]

        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["successes"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["total_tokens"], 175)
        self.assertEqual(summary["average_latency_ms"], 750)
        self.assertEqual(summary["cache_rate"], 1 / 3)


if __name__ == "__main__":
    unittest.main()
