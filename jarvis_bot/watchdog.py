from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .health import heartbeat_is_healthy
from .send_reminder import send_message_with_attachment


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HEARTBEAT_PATH = PROJECT_ROOT / "data" / "jarvis-heartbeat.json"
DEFAULT_STATE_PATH = PROJECT_ROOT / "data" / "jarvis-watchdog-state.json"
DEFAULT_OFFLINE_GIF = PROJECT_ROOT / "assets" / "jarvis-offline-hud.gif"
DEFAULT_ONLINE_GIF = PROJECT_ROOT / "assets" / "jarvis-online-hud.gif"
VALID_STATES = frozenset({"unknown", "online", "offline"})


@dataclass(slots=True)
class TransitionTracker:
    state: str = "unknown"
    failure_threshold: int = 3
    recovery_threshold: int = 2
    failures: int = 0
    successes: int = 0

    def __post_init__(self) -> None:
        if self.state not in VALID_STATES:
            self.state = "unknown"
        if self.failure_threshold <= 0 or self.recovery_threshold <= 0:
            raise ValueError("watchdog thresholds must be positive")

    def observe(self, healthy: bool) -> str | None:
        if healthy:
            self.successes += 1
            self.failures = 0
            if self.state == "offline" and self.successes >= self.recovery_threshold:
                return "online"
            if self.state == "unknown" and self.successes >= self.recovery_threshold:
                self.state = "online"
            return None

        self.failures += 1
        self.successes = 0
        if self.state != "offline" and self.failures >= self.failure_threshold:
            return "offline"
        return None

    def commit(self, state: str) -> None:
        if state not in {"online", "offline"}:
            raise ValueError(f"invalid transition: {state}")
        self.state = state
        self.failures = 0
        self.successes = 0


def load_state(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return "unknown"
    state = payload.get("state") if isinstance(payload, dict) else None
    return state if state in VALID_STATES else "unknown"


def save_state(path: Path, state: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"state": state}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def discord_token() -> str:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        token = (dotenv_values(PROJECT_ROOT / ".env").get("DISCORD_BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Discord bot token is unavailable")
    return token


def discord_is_reachable() -> bool:
    """Check actual host connectivity, independently of Discord's ready flag."""
    request = urllib.request.Request(
        "https://discord.com/api/v10/gateway",
        headers={"User-Agent": "JarvisWatchdog/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read(4096))
        return isinstance(payload, dict) and str(payload.get("url", "")).startswith("wss://")
    except Exception:
        return False


def notification_content(state: str, *, observed_at: float | None = None) -> str:
    """Keep recorded event time distinct from delivery time after a disconnect."""
    timestamp = int(time.time() if observed_at is None else observed_at)
    when = f"<t:{timestamp}:f> · <t:{timestamp}:R>"
    if state == "offline":
        return (
            "🟠 **J.A.R.V.I.S. // SIGNAL LOST**\n"
            "> Heartbeat or Discord connection interrupted.\n"
            f"-# Detected {when}\n"
            "-# Recorded outage · delivery may be delayed until the connection returns."
        )
    if state == "online":
        return (
            "🟢 **J.A.R.V.I.S. // BACK ONLINE**\n"
            "> Ready when you are, sir.\n"
            f"-# Recovered {when}\n"
            "-# Heartbeat verified · Discord reachable"
        )
    raise ValueError(f"invalid notification state: {state}")


def notify(channel_id: int, state: str, gif_path: Path, token: str, *,
           observed_at: float | None = None) -> str:
    return send_message_with_attachment(
        channel_id,
        notification_content(state, observed_at=observed_at),
        gif_path,
        token=token,
    )


class NotificationMonitor:
    """Persist observations before attempting network delivery, including recovery."""

    def __init__(self, path: Path, *, failure_threshold: int = 3,
                 recovery_threshold: int = 2) -> None:
        self.path = path
        self.tracker = TransitionTracker(load_state(path), failure_threshold, recovery_threshold)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.pending = payload.get("pending", [])
            if not isinstance(self.pending, list) or any(
                not isinstance(item, dict) or item.get("state") not in {"offline", "online"}
                or not isinstance(item.get("observed_at"), (int, float))
                for item in self.pending
            ):
                raise ValueError("Invalid notification outbox")
        except (OSError, ValueError, AttributeError):
            self.pending = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"state": self.tracker.state, "pending": self.pending},
                                        separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.path)

    def observe(self, healthy: bool, *, now: float | None = None) -> None:
        previous = self.tracker.state
        transition = self.tracker.observe(healthy)
        if transition:
            self.tracker.commit(transition)
            self.pending.append({"state": transition,
                                 "observed_at": time.time() if now is None else now})
            self.save()
            LOGGER.info("Recorded %s transition; pending=%s", transition, len(self.pending))
        elif self.tracker.state != previous:
            self.save()
            LOGGER.info("Established initial watchdog state: %s", self.tracker.state)

    def deliver(self, channel_id: int, offline_gif: Path, online_gif: Path, token: str) -> None:
        while self.pending:
            event = self.pending[0]
            gif = offline_gif if event["state"] == "offline" else online_gif
            try:
                message_id = notify(channel_id, event["state"], gif, token,
                                    observed_at=event["observed_at"])
            except Exception as error:
                LOGGER.warning("Notification retained for retry: state=%s error=%s",
                               event["state"], type(error).__name__)
                return
            self.pending.pop(0)
            self.save()
            LOGGER.info("Sent %s notification: message_id=%s", event["state"], message_id)


def run(args: argparse.Namespace) -> None:
    token = discord_token()
    monitor = NotificationMonitor(
        args.state_file,
        failure_threshold=args.failure_threshold,
        recovery_threshold=args.recovery_threshold,
    )
    LOGGER.info(
        "Watchdog started: interval=%ss heartbeat=%s state=%s",
        args.interval,
        args.heartbeat_file,
        monitor.tracker.state,
    )

    while True:
        healthy = heartbeat_is_healthy(
            args.heartbeat_file,
            maximum_age_seconds=args.maximum_heartbeat_age,
        )
        reachable = discord_is_reachable()
        monitor.observe(healthy and reachable)
        if reachable:
            monitor.deliver(args.channel_id, args.offline_gif, args.online_gif, token)

        if args.once:
            return
        time.sleep(args.interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor Jarvis heartbeats")
    parser.add_argument("--channel-id", required=True, type=int)
    parser.add_argument("--heartbeat-file", type=Path, default=DEFAULT_HEARTBEAT_PATH)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--offline-gif", type=Path, default=DEFAULT_OFFLINE_GIF)
    parser.add_argument("--online-gif", type=Path, default=DEFAULT_ONLINE_GIF)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--maximum-heartbeat-age", type=float, default=45.0)
    parser.add_argument("--failure-threshold", type=int, default=3)
    parser.add_argument("--recovery-threshold", type=int, default=2)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval <= 0 or args.maximum_heartbeat_age <= 0:
        parser.error("interval and maximum heartbeat age must be positive")
    for gif_path in (args.offline_gif, args.online_gif):
        if not gif_path.is_file():
            parser.error(f"notification GIF does not exist: {gif_path}")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run(args)


if __name__ == "__main__":
    main()
