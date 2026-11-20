"""Read-only Codex earned-reset expiry checks, independent of the Discord worker."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import math
import os
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .send_reminder import _discord_token


WARNING_HOURS = (72, 24, 6)


@dataclass(frozen=True)
class Credit:
    credit_id: str
    expires_at: int | None

    @property
    def key(self) -> str:
        return hashlib.sha256(f'{self.credit_id}:{self.expires_at}'.encode()).hexdigest()


def parse_credits(result: dict) -> tuple[int, list[Credit] | None]:
    """Keep missing detail distinct from a confirmed empty balance."""
    payload = result.get('rateLimitResetCredits')
    if not isinstance(payload, dict):
        raise ValueError('Earned-reset balance unavailable')
    count = payload.get('availableCount')
    if type(count) is not int or count < 0:
        raise ValueError('Invalid earned-reset count')
    rows = payload.get('credits')
    if rows is None:
        return count, None
    if not isinstance(rows, list):
        raise ValueError('Invalid earned-reset details')
    credits = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid earned-reset row')
        if row.get('status') != 'available' or row.get('resetType') != 'codexRateLimits':
            continue
        credit_id, expires = row.get('id'), row.get('expiresAt')
        if not isinstance(credit_id, str) or not credit_id.strip():
            raise ValueError('Invalid earned-reset identity')
        if expires is not None:
            if type(expires) is not int or not 0 < expires <= 253402300799:
                raise ValueError('Invalid earned-reset expiry')
        if credit_id in seen:
            raise ValueError('Duplicate earned-reset identity')
        seen.add(credit_id)
        credits.append(Credit(credit_id, expires))
    # availableCount is authoritative: detail rows can be capped by the service.
    if count == 0:
        return 0, []
    return count, credits


async def read_credits(binary: Path, workdir: Path) -> dict:
    """Only initialize and read limits. Never invoke a model or redeem a credit."""
    environment = os.environ.copy()
    for name in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'DISCORD_BOT_TOKEN'):
        environment.pop(name, None)
    process = await asyncio.create_subprocess_exec(
        str(binary), 'app-server', '--listen', 'stdio://', cwd=workdir,
        env=environment, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async def exchange():
            for request in (
                {'id': 0, 'method': 'initialize', 'params': {'clientInfo': {
                    'name': 'jarvis-reset-expiry-watch', 'version': '1'}}},
                {'method': 'initialized', 'params': {}},
                {'id': 1, 'method': 'account/rateLimits/read'},
            ):
                process.stdin.write((json.dumps(request) + '\n').encode())
            await process.stdin.drain()
            while line := await process.stdout.readline():
                event = json.loads(line)
                if event.get('id') != 1:
                    continue
                if event.get('error') or not isinstance(event.get('result'), dict):
                    raise RuntimeError('Codex earned-reset lookup failed')
                return event['result']
            raise RuntimeError('Codex exited without earned-reset data')
        return await asyncio.wait_for(exchange(), timeout=20)
    finally:
        if process.stdin:
            process.stdin.close()
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                process.kill()
                await process.wait()


def load_state(path: Path, owner_id: int) -> dict:
    if not path.exists():
        return {'version': 1, 'owner_id': owner_id, 'alerts': {}}
    state = json.loads(path.read_text())
    if (not isinstance(state, dict) or state.get('version') != 1
            or state.get('owner_id') != owner_id or not isinstance(state.get('alerts'), dict)):
        raise ValueError('Invalid reset-watch state; refusing to forget prior alerts')
    for entry in state['alerts'].values():
        if not isinstance(entry, dict) or entry.get('band_hours') not in WARNING_HOURS:
            raise ValueError('Invalid saved reset-watch alert')
    return state


def save_state(path: Path, state: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix='.reset-watch-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(state, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def due_alerts(credits: list[Credit], state: dict, now: float) -> list[tuple[Credit, int]]:
    due = []
    for credit in credits:
        if credit.expires_at is None:
            continue
        remaining = credit.expires_at - now
        bands = [hours for hours in WARNING_HOURS if 0 < remaining <= hours * 3600]
        if not bands:
            continue
        band = min(bands)  # Starting late sends one useful warning, not three.
        previous = state['alerts'].get(credit.key)
        if previous is None or band < previous['band_hours']:
            due.append((credit, band))
    return sorted(due, key=lambda item: item[0].expires_at)


def alert_text(credit: Credit, count: int, now: float, timezone: str) -> str:
    expiry = datetime.fromtimestamp(credit.expires_at, ZoneInfo(timezone))
    hours = max(1, math.ceil((credit.expires_at - now) / 3600))
    return (
        f'**Codex free reset expires soon:** one of your {count} available resets '
        f'expires **{expiry:%d %B %Y at %I:%M %p %Z}** '
        f'(about {hours} hour{"s" if hours != 1 else ""} remaining).\n'
        'Use it from Codex’s usage/reset controls before then if you need the capacity. '
        'This is a reminder; I have not redeemed it.'
    )


class OwnerDM:
    """Resolve and verify the single owner recipient before sending any account data."""
    def __init__(self, owner_id: int):
        self.owner_id = owner_id
        self.token = _discord_token()
        self.channel_id = None

    def request(self, path: str, payload: dict | None = None) -> dict:
        request = urllib.request.Request(
            'https://discord.com/api/v10' + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Authorization': f'Bot {self.token}', 'Content-Type': 'application/json',
                     'User-Agent': 'JarvisDiscordBot/1.0'},
            method='GET' if payload is None else 'POST',
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def send(self, content: str, nonce: str) -> str:
        if self.channel_id is None:
            created = self.request('/users/@me/channels', {'recipient_id': str(self.owner_id)})
            channel_id = created.get('id')
            if not isinstance(channel_id, str) or not channel_id.isdigit():
                raise RuntimeError('Discord returned no DM channel')
            verified = self.request('/channels/' + channel_id)
            recipients = verified.get('recipients')
            if (verified.get('id') != channel_id or verified.get('type') != 1
                    or not isinstance(recipients, list) or len(recipients) != 1
                    or str(recipients[0].get('id')) != str(self.owner_id)):
                raise RuntimeError('Discord DM recipient verification failed')
            self.channel_id = channel_id
        message = self.request(f'/channels/{self.channel_id}/messages', {
            'content': content, 'allowed_mentions': {'parse': []},
            'nonce': nonce, 'enforce_nonce': True, 'flags': 1 << 2,
        })
        if (message.get('channel_id') != self.channel_id or message.get('content') != content
                or not message.get('id')):
            raise RuntimeError('Discord DM receipt verification failed')
        return str(message['id'])


def check_once(result: dict, state_path: Path, owner_id: int, timezone: str,
               send, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    count, credits = parse_credits(result)
    state = load_state(state_path, owner_id)
    state.update(last_checked_at=now, available_count=count,
                 details_available=credits is not None,
                 expiry_dates=None if credits is None else [c.expires_at for c in credits])
    save_state(state_path, state)
    sent = []
    for credit, band in due_alerts(credits or [], state, now):
        nonce = hashlib.sha256(f'{owner_id}:{credit.key}:{band}'.encode()).hexdigest()[:24]
        message_id = send(alert_text(credit, count, now, timezone), nonce)
        state['alerts'][credit.key] = {
            'band_hours': band, 'sent_at': now, 'message_id': message_id,
        }
        save_state(state_path, state)
        sent.append(message_id)
    return {'available_count': count, 'details_available': credits is not None,
            'detail_count': None if credits is None else len(credits),
            'expiry_dates': state['expiry_dates'], 'sent_message_ids': sent}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-binary', type=Path, default=Path.home() / '.local/bin/codex')
    parser.add_argument('--workdir', type=Path, default=Path.cwd())
    parser.add_argument('--state-file', type=Path,
                        default=Path.home() / '.local/share/jarvis-reset-watch/state.json')
    parser.add_argument('--owner-id', required=True, type=int)
    parser.add_argument('--timezone', default='America/Chicago')
    parser.add_argument('--inspect', action='store_true', help='Read only; do not write state or send DMs')
    args = parser.parse_args()
    if args.owner_id <= 0:
        parser.error('owner-id must be positive')
    ZoneInfo(args.timezone)
    os.umask(0o077)
    if args.inspect:
        count, credits = parse_credits(asyncio.run(read_credits(args.codex_binary, args.workdir)))
        print(json.dumps({'available_count': count, 'details_available': credits is not None,
                          'expiry_dates': None if credits is None else [c.expires_at for c in credits]}))
        return
    args.state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with args.state_file.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = asyncio.run(read_credits(args.codex_binary, args.workdir))
        sender = OwnerDM(args.owner_id)
        print(json.dumps(check_once(result, args.state_file, args.owner_id,
                                    args.timezone, sender.send)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Error bodies can contain account data or URLs. Logs need only the class.
        print(f'Reset expiry check failed ({type(error).__name__}); no balance inferred.', flush=True)
        raise SystemExit(1)
