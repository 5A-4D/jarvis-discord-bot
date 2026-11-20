"""Host-side Docker collector and unprivileged Discord report rendering.

The collector uses Docker list/inspect/stats and fixed read-only exec probes. It publishes selected
fields, never container environment variables, mounts, command lines or logs.
This module deliberately uses only the Python standard library on the host.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .vpn_status import collect_vpn_status, render_vpn_status


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_PATH = PROJECT_ROOT / "data" / "container-status.json"
DEFAULT_EXPECTED_PATH = PROJECT_ROOT / "data" / "expected-containers.json"
DEFAULT_DOCKER = PROJECT_ROOT / "scripts" / "docker-cli"
STALE_SECONDS = 90
LOGGER = logging.getLogger(__name__)

# Project/service labels only: arbitrary labels and health-check output can
# contain credentials. Select fields within Docker before receiving any JSON.
INSPECT_FORMAT = (
    '{"name":{{json .Name}},"id":{{json .Id}},'
    '"image":{{json .Config.Image}},'
    '"stack":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"state":{{json .State.Status}},"started_at":{{json .State.StartedAt}},'
    '"network_mode":{{json .HostConfig.NetworkMode}},'
    '"finished_at":{{json .State.FinishedAt}},'
    '"health":{{with (index .State "Health")}}{{json .Status}}'
    '{{else}}"not configured"{{end}},'
    '"restart_count":{{.RestartCount}},"exit_code":{{.State.ExitCode}},'
    '"oom_killed":{{.State.OOMKilled}}}'
)
STATS_FIELDS = ("CPUPerc", "MemUsage", "MemPerc", "PIDs", "NetIO", "BlockIO")


def is_container_command(prompt: str) -> bool:
    return " ".join(prompt.casefold().split()) in {
        "containers", "/containers", "containers details", "/containers details",
        "docker status", "container status", "docker reports",
    }


def docker_command(docker: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            [str(docker), *arguments], capture_output=True, text=True,
            timeout=15, check=True,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Docker query timed out") from error
    except (OSError, subprocess.CalledProcessError) as error:
        # Do not echo arbitrary daemon stderr into Discord or the report file.
        raise RuntimeError("Docker query failed; daemon or access unavailable") from error
    return result.stdout


def collect_report(docker: Path, expected: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": 1, "checked_at": time.time(), "host": "OMEN",
        "docker_ok": False, "containers": [], "missing": [],
    }
    try:
        ids = docker_command(docker, "ps", "--all", "--quiet", "--no-trunc").split()
        containers = []
        # Batch inspect to avoid command-line length limits on large engines.
        for offset in range(0, len(ids), 50):
            raw = docker_command(
                docker, "inspect", "--type", "container", "--format",
                INSPECT_FORMAT, *ids[offset:offset + 50],
            )
            for line in raw.splitlines():
                entry = json.loads(line)
                entry["name"] = entry["name"].lstrip("/")
                containers.append(entry)
        report["containers"] = sorted(
            containers, key=lambda item: (item.get("stack") or "", item["name"])
        )
        report["docker_ok"] = True
        names = {item["name"] for item in containers}
        report["missing"] = sorted(set(expected) - names)
    except (RuntimeError, ValueError, KeyError, TypeError) as error:
        report["error"] = str(error) if isinstance(error, RuntimeError) else "Invalid Docker response"
        return report

    if ids:
        try:
            raw = docker_command(docker, "stats", "--all", "--no-stream", "--format", "{{json .}}")
            stats = {item["Name"]: item for item in map(json.loads, raw.splitlines())}
            for entry in report["containers"]:
                sample = stats.get(entry["name"], {})
                entry["resources"] = {key: sample[key] for key in STATS_FIELDS if key in sample}
        except (RuntimeError, ValueError, KeyError, TypeError):
            report["stats_error"] = "Resource metrics unavailable for this sample"
    report["vpn_protection"] = collect_vpn_status(
        lambda *args: docker_command(docker, *args), report["containers"]
    )
    # Timestamp the inventory sample, not the end of a potentially slow stats call.
    return report


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(payload, stream, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clean(value: Any, limit: int = 160) -> str:
    # Prevent mentions, Markdown injection and multiline fields from Docker names.
    text = " ".join(str(value if value is not None else "—").split())
    return text.translate(str.maketrans({"`": "'", "@": "＠", "*": "", "_": "＿", "~": ""}))[:limit]


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"


def elapsed(timestamp: Any, checked_at: float) -> str:
    try:
        started = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if started.year < 1970 or started.tzinfo is None:
            return "unknown"
        return duration(checked_at - started.timestamp())
    except (ValueError, TypeError, OverflowError):
        return "unknown"


def render_report(payload: dict[str, Any], *, now: float | None = None) -> str:
    checked_at = payload.get("checked_at")
    if (
        payload.get("schema_version") != 1
        or not isinstance(checked_at, (float, int))
        or isinstance(checked_at, bool)
        or not math.isfinite(checked_at)
        or not isinstance(payload.get("containers"), list)
        or not isinstance(payload.get("missing"), list)
        or not isinstance(payload.get("docker_ok"), bool)
    ):
        raise ValueError("Invalid container report")
    age = (time.time() if now is None else now) - checked_at
    observed = datetime.fromtimestamp(checked_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    stale = age < -5 or age > STALE_SECONDS
    lines = [
        f"**OMEN containers{' — STALE REPORT' if stale else ''}**",
        f"Checked {duration(max(0, age))} ago · {observed}",
    ]
    if stale:
        lines.append("⚠️ Collector data is stale or its clock is incorrect; current status is unknown.")
    if not payload["docker_ok"]:
        lines.append("🔴 Docker status unavailable. The collector could not query the daemon.")
        return "\n".join(lines)

    containers = payload["containers"]
    running = sum(item.get("state") == "running" for item in containers)
    lines.append(f"{len(containers)} discovered · {running} running · {len(payload['missing'])} expected missing")
    lines.extend(render_vpn_status(payload.get("vpn_protection"), stale=stale))
    for item in containers:
        state, health = item.get("state", "unknown"), item.get("health", "not configured")
        icon = (
            "⚪" if stale else
            "🔴" if state != "running" or health == "unhealthy" else
            "🟢" if health == "healthy" else "🟡"
        )
        lines.extend([
            "",
            f"{icon} **{clean(item['name'])}** · {clean(state)} · health: {clean(health)}",
            f"Stack: {clean(item.get('stack') or 'standalone')} · image: {clean(item.get('image'))}",
        ])
        if state in {"running", "paused", "restarting"}:
            lines.append(f"Uptime: {elapsed(item.get('started_at'), checked_at)} · restarts: {clean(item.get('restart_count'))}")
        else:
            lines.append(f"Stopped: {elapsed(item.get('finished_at'), checked_at)} ago · exit: {clean(item.get('exit_code'))} · restarts: {clean(item.get('restart_count'))}")
        if item.get("oom_killed"):
            lines.append("⚠️ Docker reports an out-of-memory kill.")
        resource = item.get("resources") or {}
        if state == "running" and resource:
            lines.append(
                f"CPU: {clean(resource.get('CPUPerc'))} · RAM: {clean(resource.get('MemUsage'))} ({clean(resource.get('MemPerc'))}) · PIDs: {clean(resource.get('PIDs'))}"
            )
            lines.append(f"Network I/O: {clean(resource.get('NetIO'))} · disk I/O: {clean(resource.get('BlockIO'))}")
        elif state == "running":
            lines.append("Resource metrics unavailable.")
    for name in payload["missing"]:
        lines.extend(["", f"🔴 **{clean(name)}** · expected container missing"])
    if not containers and not payload["missing"]:
        lines.append("Docker is reachable and has no containers.")
    lines.extend(["", "Container health reflects Docker checks. VPN/protection evidence is listed separately above; 🟡 may mean no app health check is configured."])
    return "\n".join(lines)


def read_container_report(path: Path = DEFAULT_REPORT_PATH) -> str:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Report too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return render_report(payload)
    except FileNotFoundError:
        return "Container status is unavailable: the OMEN collector has not published a report yet."
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return "Container status is unavailable: the saved report could not be read or validated."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", type=Path, default=DEFAULT_DOCKER)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--expected", type=Path, default=DEFAULT_EXPECTED_PATH)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("interval must be positive and finite")
    logging.basicConfig(level=logging.INFO)
    while True:
        started = time.monotonic()
        try:
            expected = json.loads(args.expected.read_text(encoding="utf-8"))
            if not isinstance(expected, list) or not all(isinstance(name, str) for name in expected):
                raise ValueError("Expected-container list must contain names")
            report = collect_report(args.docker, expected)
            write_report(args.output, report)
            if not report["docker_ok"]:
                LOGGER.warning("Docker status query failed")
        except (OSError, ValueError):
            # Leave old reports to expire visibly, never present them as fresh.
            LOGGER.exception("Could not publish container report")
            if args.once:
                raise
        if args.once:
            return
        time.sleep(max(0.1, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
