#!/usr/bin/env python3
"""Host-side nightly maintenance controller; install a trusted copy outside the repo.

The editing container receives source and its own Codex login only. It never gets
the bot token, live data, Docker socket, home directory, or systemd bus. The host
controller owns publication, source conflict checks, backups, reporting and rollback.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.home() / '.local/share/jarvis-maintenance'
INSTALL = Path.home() / '.local/lib/jarvis-maintenance'
PROJECT = Path(os.environ.get(
    'JARVIS_PROJECT_DIR', str(Path.home() / 'Projects/jarvis-discord-bot'),
)).expanduser()
DOCKER = PROJECT / 'scripts/docker-cli'
RESTART = PROJECT / 'scripts/restart-after-reply'
BOT = 'jarvis-discord'
CHANNEL = '900000000000000006'
TOP = {'main.py', 'requirements.txt', 'README.md', 'Dockerfile', 'compose.yaml',
       '.env.example', '.gitignore', '.dockerignore', 'jarvis-watch'}
TREES = {'jarvis_bot', 'tests', 'scripts', 'assets'}
EDITABLE = {'jarvis_bot/progress.py', 'jarvis_bot/telemetry.py'}
SCHEMA = {'type': 'object', 'additionalProperties': False,
          'properties': {k: {'type': 'string'} for k in ('title', 'summary', 'evidence')},
          'required': ['title', 'summary', 'evidence']}
REVIEW_SCHEMA = {'type': 'object', 'additionalProperties': False,
                 'properties': {'approved': {'type': 'boolean'}, 'reason': {'type': 'string'}},
                 'required': ['approved', 'reason']}
RUN_DEADLINE: float | None = None
STAGE_LABELS = {
    'checking_prerequisites': 'startup prerequisites',
    'checking_backup_and_health': 'backup and health checks',
    'testing_baseline': 'baseline tests (before implementation)',
    'implementing': 'implementation',
    'testing_candidate': 'candidate tests',
    'reviewing': 'independent review',
    'building': 'image build',
    'testing_built_image': 'packaged-image tests',
    'publishing': 'publication',
    'deployment_queued': 'queued deployment',
}


class StopRun(Exception):
    """Only fixed, content-free reason codes belong in this exception."""


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    with tmp.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.chmod(0o600)
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def verify_golden(golden: Path) -> None:
    """Check the recovery files themselves, without reading private content to logs."""
    manifest = read_json(golden / 'manifest.json')
    if manifest.get('sqlite_integrity') != 'ok' or not manifest.get('databases'):
        raise StopRun('golden_snapshot_unverified')
    for name, record in manifest['files'].items():
        path = golden / name
        if not path.resolve().is_relative_to(golden.resolve()) or path.is_symlink():
            raise StopRun('golden_snapshot_unverified')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        if path.stat().st_size != record['bytes'] or digest.hexdigest() != record['sha256']:
            raise StopRun('golden_snapshot_unverified')
    for name in manifest['databases']:
        path = golden / 'private' / 'data' / name
        if not path.resolve().is_relative_to(golden.resolve()):
            raise StopRun('golden_snapshot_unverified')
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as database:
            if database.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise StopRun('golden_snapshot_unverified')


@contextmanager
def lock(name: str):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / name).open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StopRun('already_running') from None
        yield


def command(args: list[str], *, timeout=120, cwd=None, log: Path | None = None,
            input_text: str | None = None) -> str:
    if RUN_DEADLINE is not None:
        timeout = min(timeout, RUN_DEADLINE - time.monotonic())
        if timeout <= 0:
            raise StopRun('runtime_limit_reached')
    try:
        result = subprocess.run(args, cwd=cwd, input=input_text, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise StopRun('command_timeout') from None
    if log is not None:
        log.write_text(result.stdout)
        log.chmod(0o600)
    if result.returncode:
        raise StopRun('command_failed')
    return result.stdout.strip()


def docker(*args: str, **kwargs) -> str:
    return command([str(DOCKER), *args], **kwargs)


def inventory(project: Path) -> dict[str, str]:
    """Source only; no links, databases, credentials, runtime state or outputs."""
    result = {}
    for path in sorted(project.rglob('*')):
        rel = path.relative_to(project)
        if rel.parts[0] not in TOP | TREES:
            continue
        if any(p in {'__pycache__', '.pytest_cache', '.git'} for p in rel.parts):
            continue
        if path.is_symlink():
            raise StopRun('source_symlink')
        if not path.is_file() or path.suffix in {'.pyc', '.pyo'}:
            continue
        if path.suffix in {'.key', '.pem', '.sqlite3', '.db'} or (
                path.name.startswith('.env') and path.name != '.env.example'):
            raise StopRun('unexpected_sensitive_source')
        result[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def check_patch(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    if not changed:
        raise StopRun('no_improvement_produced')
    production = False
    for name in changed:
        if re.search(r'calendar|schedul|availability|huddle|(?:^|[_/])ics(?:[_./]|$)', name.casefold()):
            raise StopRun('schedule_improvements_excluded')
        if name not in after:
            raise StopRun('file_deletion_rejected')
        if name in EDITABLE:
            production = True
        elif name == 'README.md':
            pass
        elif re.fullmatch(r'tests/test_[a-z0-9_]+\.py', name) and name not in before:
            pass  # Existing regression tests cannot be weakened by the worker.
        elif re.fullmatch(r'jarvis_bot/[a-z][a-z0-9_]+\.py', name) and name not in before:
            production = True
        else:
            raise StopRun('protected_file_change')
    if not production:
        raise StopRun('production_improvement_required')
    if not any(n.startswith('tests/test_') and n not in before for n in changed):
        raise StopRun('new_regression_test_required')
    return changed


def remaining(result: dict) -> tuple[float, float]:
    buckets = result.get('rateLimitsByLimitId') or {}
    bucket = buckets.get('codex') or result.get('rateLimits') or {}
    values = []
    for key in ('primary', 'secondary'):
        used = bucket.get(key, {}).get('usedPercent')
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
            raise StopRun('quota_unavailable')
        values.append(max(0., min(100., 100. - used)))
    return tuple(values)


def read_quota(binary: Path) -> tuple[float, float]:
    env = os.environ.copy()
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'DISCORD_BOT_TOKEN'):
        env.pop(key, None)
    p = subprocess.Popen([str(binary), 'app-server', '--listen', 'stdio://'],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=env)
    try:
        for message in (
            {'id': 0, 'method': 'initialize', 'params': {'clientInfo': {
                'name': 'jarvis-nightly', 'version': '1'}}},
            {'method': 'initialized', 'params': {}},
            {'id': 1, 'method': 'account/rateLimits/read'}):
            p.stdin.write((json.dumps(message) + '\n').encode())
        p.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(p.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 20
        buffer = b''
        while time.monotonic() < deadline:
            if not selector.select(timeout=max(0, deadline-time.monotonic())):
                break
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > 1024 * 1024:
                break
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                event = json.loads(line)
                if event.get('id') == 1:
                    return remaining(event.get('result', {}))
        raise StopRun('quota_unavailable')
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


def container_state() -> tuple[str, str]:
    # Never inspect/print the live container's credential-bearing environment.
    value = docker('inspect', '--format',
                   '{{.Image}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}', BOT)
    return tuple(value.split())


def source_backup(run: Path, before: dict[str, str], image: str) -> None:
    """Every-night code/image checkpoint; the private DB backup is golden-only."""
    run.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tarfile.open(run / 'source-before.tar.gz', 'w:gz') as archive:
        for name in before:
            archive.add(PROJECT / name, arcname=name, recursive=False)
    tag = 'jarvis-discord:nightly-before-' + run.name.lower()
    docker('image', 'tag', image, tag)
    write_json(run / 'baseline.json', {'files': before, 'image': image, 'image_tag': tag})


def sandbox_args(image: str, workspace: Path, *, name: str,
                 binary: Path | None = None, auth: Path | None = None,
                 writable: bool = False) -> list[str]:
    args = ['run', '--rm', '--name', name, '--label', 'io.jarvis.maintenance=true',
            '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--user', '1000:1000',
            '--pids-limit', '256', '--memory', '3g', '--cpus', '2',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m,mode=1777',
            '--mount', f'type=bind,source={workspace},target=/workspace' + ('' if writable else ',readonly'),
            '--workdir', '/workspace', '--env', 'PYTHONPATH=/workspace',
            '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'HOME=/tmp/home',
            '--env', 'TZ=America/Chicago']
    if binary is not None:
        args += ['--interactive', '--network', 'bridge', '--mount',
                 f'type=bind,source={binary},target=/usr/local/bin/codex,readonly',
                 '--mount', f'type=bind,source={auth},target=/codex-home',
                 '--env', 'CODEX_HOME=/codex-home', '--entrypoint', '/usr/local/bin/codex']
        # Modern Codex dispatches execution through sibling runtime binaries.
        # Mount executables individually; never expose the containing host tree.
        for sibling in ('codex-code-mode-host', 'codex-exec-server'):
            helper = binary.parent / sibling
            if helper.is_file():
                args += ['--mount', f'type=bind,source={helper},target=/usr/local/bin/{sibling},readonly']
    else:
        args += ['--network', 'none', '--entrypoint', '/opt/venv/bin/python']
    args.append(image)
    return args


def run_agent(run: Path, image: str, binary: Path, prompt: str, *, review=False) -> dict:
    workspace = run / 'workspace'
    auth = run / 'codex-home'
    name = 'jarvis-nightly-' + run.name.lower() + ('-review' if review else '-edit')
    schema_name = 'review-schema.json' if review else 'improvement-schema.json'
    schema = REVIEW_SCHEMA if review else SCHEMA
    # Schema lives in private Codex home; never deploy model output or credentials.
    write_json(auth / schema_name, schema)
    output = 'review.json' if review else 'result.json'
    args = sandbox_args(image, workspace, name=name, binary=binary, auth=auth, writable=not review)
    args += ['--enable', 'code_mode_host', '--disable', 'apps', '--disable', 'plugins',
             '--disable', 'multi_agent', '--disable', 'hooks',
             '--disable', 'computer_use', '--disable', 'browser_use',
             '--disable', 'image_generation', '-a', 'never', 'exec', '--ephemeral',
             '--ignore-user-config',
             # Docker is the external OS sandbox; nested user namespaces are
             # unavailable. The review workspace is also mounted read-only.
             '--skip-git-repo-check', '--dangerously-bypass-approvals-and-sandbox',
             '--cd', '/workspace', '--output-schema', '/codex-home/' + schema_name,
             '-o', '/codex-home/' + output, '-']
    policy = read_json(INSTALL / 'policy.json')
    if policy.get('model'):
        args[-1:-1] = ['--model', policy['model']]
    if policy.get('reasoning_effort'):
        args[-1:-1] = ['-c', 'model_reasoning_effort=' + json.dumps(policy['reasoning_effort'])]
    try:
        # Monitor quota while the model runs; a post-run check alone can spend
        # the entire morning reserve before noticing the limit.
        log = run / ('review.log' if review else 'worker.log')
        limit = time.monotonic() + (300 if review else 2700)
        if RUN_DEADLINE is not None:
            limit = min(limit, RUN_DEADLINE)
        with log.open('w') as stream:
            process = subprocess.Popen([str(DOCKER), *args], stdin=subprocess.PIPE,
                       stdout=stream, stderr=subprocess.STDOUT, text=True)
            try:
                process.stdin.write(prompt)
                process.stdin.close()
                while process.poll() is None:
                    if time.monotonic() >= limit:
                        raise StopRun('runtime_limit_reached')
                    try:
                        process.wait(timeout=min(60, max(1, limit-time.monotonic())))
                    except subprocess.TimeoutExpired:
                        policy = read_json(INSTALL / 'policy.json')
                        quota = read_quota(binary)
                        if quota[0] <= policy['minimum_after_run_remaining'] or quota[1] <= policy['minimum_week_remaining']:
                            raise StopRun('quota_reserve_reached')
                if process.returncode:
                    raise StopRun('worker_failed')
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        value = read_json(auth / output)
        if not isinstance(value, dict):
            raise StopRun('invalid_worker_result')
        return value
    finally:
        subprocess.run([str(DOCKER), 'rm', '-f', name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)


def tests(run: Path, image: str, workspace: Path, label: str, *, built=False) -> None:
    args = sandbox_args(image, workspace, name='jarvis-check-' + uuid.uuid4().hex[:12])
    if built:
        # The built-image check imports installed code, not the workspace overlay.
        pos = args.index('--workdir')
        args[pos + 1] = '/opt/jarvis'
        pos = args.index('PYTHONPATH=/workspace')
        args[pos] = 'PYTHONPATH=/opt/jarvis'
    args += ['-m', 'unittest', 'discover', '-s', 'tests', '-q']
    try:
        docker(*args, timeout=300, log=run / (label + '.log'))
    except StopRun as error:
        if str(error) == 'command_failed':
            reason = {'baseline-tests': 'baseline_tests_failed',
                      'candidate-tests': 'candidate_tests_failed',
                      'built-image-tests': 'packaged_image_tests_failed'}.get(label, 'test_suite_failed')
            raise StopRun(reason) from None
        raise


def safe_text(value: str, *, limit=350) -> str:
    if re.search(r'BEGIN:(?:VCALENDAR|VEVENT)', str(value), re.I):
        return '[calendar content withheld]'
    value = re.sub(r'(?:https?|webcal)://\S+|\beyJ[A-Za-z0-9_.-]+|[A-Za-z0-9_-]{45,}', '[redacted]', str(value))
    return value.replace('@', '@\u200b').replace('`', "'").replace('<', '').replace('>', '')[:limit]


def report(run: Path, status: str, *, title='', summary='', evidence='', reason='') -> None:
    stage = None
    try:
        current = read_json(run / 'progress.json').get('stage')
        if isinstance(current, str) and current in STAGE_LABELS:
            stage = current
    except (OSError, ValueError, AttributeError):
        pass
    write_json(run / 'report.json', {'run': run.name, 'status': status,
               'title': safe_text(title, limit=100), 'summary': safe_text(summary),
               'evidence': safe_text(evidence), 'reason': reason, 'stage': stage, 'sent': False})


def progress(run: Path, stage: str) -> None:
    write_json(run / 'progress.json', {'stage': stage, 'updated_at': time.time()})


def retain_runs() -> None:
    """Bound local growth; preserve recent runs, successful versions and recovery."""
    runs = sorted((ROOT / 'runs').iterdir(), reverse=True)
    keep = set(runs[:14])
    successes = [r for r in runs if (r / 'report.json').exists()
                 and read_json(r / 'report.json').get('status') == 'deployed']
    keep.update(successes[:3])
    deployment = ROOT / 'deployment.json'
    if deployment.exists():
        keep.add(Path(read_json(deployment)['run']))
    for run in runs:
        record = run / 'report.json'
        if run in keep or not record.exists() or not read_json(record).get('sent'):
            continue
        if not re.fullmatch(r'\d{8}T\d{6}Z', run.name) or run.is_symlink():
            continue
        # Never prune unrelated Docker resources or images globally.
        for prefix in ('nightly-', 'nightly-before-'):
            subprocess.run([str(DOCKER), 'image', 'rm', 'jarvis-discord:' + prefix + run.name.lower()],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        shutil.rmtree(run)


def discord_api(path: str, token: str, payload=None):
    request = urllib.request.Request('https://discord.com/api/v10' + path,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={'Authorization': 'Bot ' + token, 'Content-Type': 'application/json',
                         'User-Agent': 'JarvisNightly/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError:
        raise StopRun('notification_failed') from None


def flush_reports() -> None:
    from dotenv import dotenv_values
    token = dotenv_values(PROJECT / '.env').get('DISCORD_BOT_TOKEN') or ''
    if not token:
        return
    for path in sorted((ROOT / 'runs').glob('*/report.json')):
        record = read_json(path)
        if record.get('sent'):
            continue
        content = '**Nightly improvement · ' + record['run'] + ' UTC**\n'
        content += '**' + record['status'].replace('_', ' ').capitalize() + '**'
        if record.get('title'):
            content += ' — ' + record['title']
        stage = record.get('stage')
        if record['status'] != 'deployed' and isinstance(stage, str) and stage in STAGE_LABELS:
            content += '\nStopped at: ' + STAGE_LABELS[stage] + '.'
        if record.get('summary'):
            content += '\n' + record['summary']
        if record.get('evidence'):
            content += '\nValidation: ' + record['evidence']
        if record.get('reason'):
            content += '\nReason: ' + record['reason'].replace('_', ' ') + '.'
        if record['status'] == 'deployed':
            content += '\nExisting and new tests passed; independent review and live health checks passed.'
        content += '\n-# Golden backup retained. No credentials or private history included.'
        try:
            message_id = record.get('message_id')
            if not message_id:
                # Reconcile a previous accepted POST whose response was lost.
                recent = discord_api('/channels/' + CHANNEL + '/messages?limit=100', token)
                previous = next((m for m in recent if m.get('author', {}).get('bot')
                                 and m.get('content') == content), None)
                if previous:
                    message_id = previous['id']
                else:
                    posted = discord_api('/channels/' + CHANNEL + '/messages', token,
                        {'content': content, 'allowed_mentions': {'parse': []},
                         # A recovered deployment can change a previously
                         # reported failure into success. Deduplicate the same
                         # report, not every outcome of the entire run.
                         'nonce': 'jn-' + hashlib.sha256(content.encode()).hexdigest()[:20],
                         'enforce_nonce': True})
                    message_id = posted['id']
                record['message_id'] = message_id
                write_json(path, record)
            verified = discord_api('/channels/' + CHANNEL + '/messages/' + str(message_id), token)
            if verified.get('content') != content or str(verified.get('channel_id')) != CHANNEL:
                record.pop('message_id', None)
                write_json(path, record)
                raise StopRun('notification_verification_failed')
            record['sent'] = True
            write_json(path, record)
        except Exception:
            continue  # Durable outbox retries on the independent supervisor timer.


def queue_restart() -> None:
    output = command([str(RESTART)], timeout=30)
    if not any(line.startswith('Queued ') for line in output.splitlines()):
        raise StopRun('restart_not_queued')


def build_candidate(run: Path, image: str, files: dict[str, str]) -> str:
    """Build from a verified local tag; Dockerfiles treat raw IDs as remote names."""
    base = 'jarvis-discord:nightly-before-' + run.name.lower()
    if docker('image', 'inspect', '--format', '{{.Id}}', base) != image:
        raise StopRun('baseline_image_changed')
    context = run / 'build'
    context.mkdir()
    for name in files:
        dst = context / 'src' / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(run / 'workspace' / name, dst)
    (context / 'Dockerfile').write_text('FROM ' + base + '\nCOPY --chown=1000:1000 src/ /opt/jarvis/\n')
    tag = 'jarvis-discord:nightly-' + run.name.lower()
    docker('build', '--network=none', '-t', tag, str(context), timeout=300, log=run/'build.log')
    return docker('image', 'inspect', '--format', '{{.Id}}', tag)


def deploy_candidate(run: Path, candidate: str, previous: str, changed: list[str],
                     before: dict[str, str], result: dict) -> None:
    if inventory(PROJECT) != before:
        raise StopRun('live_source_changed_during_run')
    with lock('deployment.lock'):
        statefile = ROOT / 'deployment.json'
        if statefile.exists() and read_json(statefile).get('phase') not in {'deployed', 'rolled_back', 'deferred'}:
            raise StopRun('deployment_pending')
        # Original source files are retained per run for conflict-safe rollback.
        originals = run / 'originals'
        for name in changed:
            src = PROJECT / name
            if src.exists():
                target = originals / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
        state = {'phase': 'prepared', 'run': str(run), 'candidate': candidate,
                 'previous': previous, 'changed': changed, 'before': before,
                 'after': inventory(run / 'workspace'), 'queued_at': time.time(),
                 'result': {k: safe_text(result.get(k, '')) for k in ('title', 'summary', 'evidence')}}
        write_json(statefile, state)  # Recovery intent is durable BEFORE publication.
        for name in changed:
            src = run / 'workspace' / name
            dst = PROJECT / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name('.' + dst.name + '.nightly')
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
        docker('image', 'tag', candidate, 'jarvis-discord:local')
        queue_restart()
        state['phase'] = 'queued'
        write_json(statefile, state)


def restore_source(state: dict) -> bool:
    """Do not overwrite a subsequent human edit while undoing our own patch."""
    run = Path(state['run'])
    conflict = False
    for name in state['changed']:
        dst = PROJECT / name
        if not dst.is_file() or hashlib.sha256(dst.read_bytes()).hexdigest() != state['after'].get(name):
            current = hashlib.sha256(dst.read_bytes()).hexdigest() if dst.is_file() else None
            conflict |= current != state['before'].get(name)
            continue
        old = run / 'originals' / name
        if old.exists():
            shutil.copy2(old, dst)
        elif name not in state['before']:
            dst.unlink()
    return conflict


def supervise() -> None:
    with lock('deployment.lock'):
        path = ROOT / 'deployment.json'
        if path.exists():
            state = read_json(path)
            phase = state['phase']
            if phase not in {'deployed', 'rolled_back', 'deferred'}:
                run = Path(state['run'])
                image, health = container_state()
                now = time.time()
                if phase == 'rolling_back':
                    if image == state['previous'] and health == 'healthy':
                        state['phase'] = 'rolled_back'
                        report(run, 'rolled_back', reason=state.get('rollback_reason', 'candidate_failed_health'))
                    elif now - state.get('rollback_queued_at', 0) >= 180:
                        # Retry recovery after a failed/reboot-interrupted restart.
                        docker('image', 'tag', state['previous'], 'jarvis-discord:local')
                        state['source_restore_conflict'] = restore_source(state)
                        queue_restart()
                        state['rollback_queued_at'] = now
                elif image == state['candidate']:
                    state.setdefault('candidate_seen_at', now)
                    if health == 'healthy':
                        state.setdefault('healthy_since', now)
                        if now - state['healthy_since'] >= 60:
                            state['phase'] = 'deployed'
                            # Commit ONLY this verified patch, not unrelated working files.
                            try:
                                command(['git', 'add', '--', *state['changed']], cwd=PROJECT)
                                command(['git', 'commit', '--only', '-m', 'Nightly: ' + state['result']['title'],
                                         '--', *state['changed']], cwd=PROJECT)
                            except StopRun:
                                pass
                            report(run, 'deployed', **state['result'])
                    else:
                        state.pop('healthy_since', None)
                    if state['phase'] != 'deployed' and (health == 'unhealthy' or now-state['candidate_seen_at'] > 180):
                        state['phase'] = 'rolling_back'
                        state['rollback_reason'] = 'candidate_failed_health'
                        write_json(path, state)
                        docker('image', 'tag', state['previous'], 'jarvis-discord:local')
                        state['source_restore_conflict'] = restore_source(state)
                        queue_restart()
                        state['rollback_queued_at'] = now
                elif now - state['queued_at'] > 900:
                    docker('image', 'tag', state['previous'], 'jarvis-discord:local')
                    restore_source(state)
                    state['phase'] = 'deferred'
                    report(run, 'implemented_not_deployed', **state['result'], reason='restart_or_publication_not_completed')
                elif phase == 'prepared':
                    # Publication may have been interrupted; prefer the known-good image.
                    docker('image', 'tag', state['previous'], 'jarvis-discord:local')
                    restore_source(state)
                    state['phase'] = 'deferred'
                    report(run, 'implemented_not_deployed', reason='publication_interrupted')
                write_json(path, state)
    flush_reports()
    retain_runs()


def startup_paths() -> tuple[dict, Path, Path]:
    """Resolve the current CLI once per run, without exposing provider errors."""
    try:
        policy = read_json(INSTALL / 'policy.json')
    except (OSError, ValueError):
        raise StopRun('maintenance_policy_unavailable') from None
    try:
        binary = Path(policy['codex_binary']).resolve(strict=True)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise OSError
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        raise StopRun('codex_binary_unavailable') from None
    try:
        pointer = (ROOT / 'golden-path').read_text().strip()
        if not pointer:
            raise ValueError
        golden = Path(pointer)
    except (OSError, ValueError):
        raise StopRun('golden_snapshot_unavailable') from None
    return policy, binary, golden


def run_nightly(*, check_only=False) -> dict:
    global RUN_DEADLINE
    with lock('nightly.lock'):
        if check_only:
            policy, binary, golden = startup_paths()
            verify_golden(golden)
            image, health = container_state()
            if health != 'healthy':
                raise StopRun('live_bot_unhealthy')
            quota = read_quota(binary)
            return {'live_health': health, 'golden_verified': True,
                    'five_hour_remaining': quota[0], 'week_remaining': quota[1],
                    'codex_binary_present': binary.is_file()}
        run = ROOT / 'runs' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        run.mkdir(parents=True, mode=0o700)
        RUN_DEADLINE = time.monotonic() + 2700
        try:
            progress(run, 'checking_prerequisites')
            policy, binary, golden = startup_paths()
            progress(run, 'checking_backup_and_health')
            verify_golden(golden)
            deployment = ROOT / 'deployment.json'
            if deployment.exists() and read_json(deployment).get('phase') not in {'deployed', 'rolled_back', 'deferred'}:
                raise StopRun('deployment_pending')
            image, health = container_state()
            if health != 'healthy':
                raise StopRun('live_bot_unhealthy')
            before = inventory(PROJECT)
            source_backup(run, before, image)
            quota = read_quota(binary)
            if quota[0] < policy['minimum_five_hour_remaining'] or quota[1] < policy['minimum_week_remaining']:
                raise StopRun('quota_below_reserve')
            workspace = run / 'workspace'
            for name in before:
                dest = workspace / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(PROJECT / name, dest)
            progress(run, 'testing_baseline')
            tests(run, image, workspace, 'baseline-tests')
            auth = run / 'codex-home'
            auth.mkdir(mode=0o700)
            shutil.copy2(Path('/home/your-user/.codex/auth.json'), auth / 'auth.json')
            (auth / 'auth.json').chmod(0o600)
            brief = (INSTALL / 'brief.txt').read_text()
            prompt = (INSTALL / 'worker-prompt.txt').read_text() + '\n\nOWNER FEEDBACK:\n' + brief
            progress(run, 'implementing')
            result = run_agent(run, image, binary, prompt)
            write_json(run / 'worker-result.json', result)
            after = inventory(workspace)
            changed = check_patch(before, after)
            if sum((workspace / name).stat().st_size for name in changed) > 250_000:
                raise StopRun('patch_too_large_for_unattended_review')
            progress(run, 'testing_candidate')
            tests(run, image, workspace, 'candidate-tests')
            review_prompt = (INSTALL / 'review-prompt.txt').read_text() + '\nChanged files:\n' + '\n'.join(changed)
            # Keep the old versions available to the independent read-only review.
            original = workspace / 'baseline-review'
            for name in changed:
                if name in before:
                    target = original / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(PROJECT / name, target)
            progress(run, 'reviewing')
            review = run_agent(run, image, binary, review_prompt, review=True)
            write_json(run / 'review-result.json', review)
            shutil.rmtree(original)
            if review.get('approved') is not True or inventory(workspace) != after:
                raise StopRun('independent_review_rejected')
            write_json(run / 'reviewed-inventory.json', after)
            # Clean source snapshot includes tests/controller added since the image
            # was built. All protected files were checked against the baseline.
            progress(run, 'building')
            candidate = build_candidate(run, image, after)
            progress(run, 'testing_built_image')
            tests(run, candidate, workspace, 'built-image-tests', built=True)
            # Quota is checked again before publication; the worker cannot spend indefinitely.
            quota = read_quota(binary)
            if quota[0] < policy['minimum_after_run_remaining'] or quota[1] < policy['minimum_week_remaining']:
                raise StopRun('quota_reserve_reached')
            progress(run, 'publishing')
            deploy_candidate(run, candidate, image, changed, before, result)
            progress(run, 'deployment_queued')
            return {'status': 'deployment_queued', 'run': run.name}
        except Exception as error:
            reason = str(error) if isinstance(error, StopRun) else 'controller_error'
            pending = ROOT / 'deployment.json'
            if pending.exists() and read_json(pending).get('run') == str(run):
                # Publication may have happened. Only the independent supervisor
                # can determine its outcome and announce success or recovery.
                return {'status': 'recovery_pending', 'reason': reason}
            report(run, 'not_deployed', reason=reason)
            return {'status': 'not_deployed', 'reason': reason}
        finally:
            RUN_DEADLINE = None
            shutil.rmtree(run / 'codex-home', ignore_errors=True)
            flush_reports()


def cleanup() -> None:
    ids = docker('ps', '-aq', '--filter', 'label=io.jarvis.maintenance=true').splitlines()
    if ids:
        docker('rm', '-f', *ids)
    for path in (ROOT / 'runs').glob('*/codex-home'):
        shutil.rmtree(path, ignore_errors=True)
    deployment = ROOT / 'deployment.json'
    pending_run = read_json(deployment).get('run') if deployment.exists() else None
    for run in (ROOT / 'runs').iterdir():
        if run.is_dir() and str(run) != pending_run and not (run / 'report.json').exists():
            report(run, 'not_deployed', reason='worker_interrupted')


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('run', 'check', 'supervise', 'cleanup'))
    args = parser.parse_args()
    try:
        if args.mode == 'cleanup':
            cleanup()
        elif args.mode == 'supervise':
            supervise()
        else:
            print(json.dumps(run_nightly(check_only=args.mode == 'check')))
    except Exception as error:
        # Exceptions from providers or subprocesses can contain credentials.
        print(json.dumps({'status': 'failed', 'reason': str(error) if isinstance(error, StopRun) else type(error).__name__}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
