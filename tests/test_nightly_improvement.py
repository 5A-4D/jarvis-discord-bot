"""Safety gates are tested independently of generated nightly changes."""
import importlib.util
import hashlib
import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

SPEC = importlib.util.spec_from_file_location(
    'nightly_controller', Path(__file__).resolve().parents[1] / 'scripts' / 'nightly_improvement.py')
nightly = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nightly)


class NightlyGateTests(unittest.TestCase):
    def baseline(self):
        return {'jarvis_bot/work_calendar.py': 'old', 'jarvis_bot/progress.py': 'old',
                'jarvis_bot/config.py': 'protected',
                'tests/test_work_calendar.py': 'original-tests', 'README.md': 'docs'}

    def valid(self):
        result = self.baseline()
        result['jarvis_bot/progress.py'] = 'new'
        result['tests/test_progress_regression_extra.py'] = 'regression'
        return result

    def test_functional_change_and_new_regression_test_accepted(self):
        self.assertEqual(len(nightly.check_patch(self.baseline(), self.valid())), 2)

    def test_no_change_and_cosmetic_only_rejected(self):
        for result in (self.baseline(), {**self.baseline(), 'README.md': 'more docs'}):
            with self.assertRaises(nightly.StopRun):
                nightly.check_patch(self.baseline(), result)

    def test_failed_test_commands_identify_the_validation_stage(self):
        for label, expected in (('baseline-tests', 'baseline_tests_failed'),
                                ('candidate-tests', 'candidate_tests_failed'),
                                ('built-image-tests', 'packaged_image_tests_failed')):
            with self.subTest(label=label), patch.object(
                    nightly, 'docker', side_effect=nightly.StopRun('command_failed')):
                with self.assertRaisesRegex(nightly.StopRun, '^' + expected + '$'):
                    nightly.tests(Path('/run'), 'image', Path('/source'), label)

    def test_validation_timeouts_are_not_misreported_as_assertion_failures(self):
        with patch.object(nightly, 'docker', side_effect=nightly.StopRun('command_timeout')):
            with self.assertRaisesRegex(nightly.StopRun, '^command_timeout$'):
                nightly.tests(Path('/run'), 'image', Path('/source'), 'baseline-tests')

    def test_failure_report_includes_safe_stage_without_forwarding_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'runs/test-run'
            nightly.progress(run, 'testing_baseline')
            (run / 'baseline-tests.log').write_text('PRIVATE ASSERTION DETAILS')
            nightly.report(run, 'not_deployed', reason='baseline_tests_failed')
            posted = {}
            def api(path, token, payload=None):
                if payload is not None:
                    posted.update(payload)
                    return {'id': 'message'}
                if '?limit=' in path:
                    return []
                return {'content': posted['content'], 'channel_id': nightly.CHANNEL}
            with patch.object(nightly, 'ROOT', root), \
                 patch('dotenv.dotenv_values', return_value={'DISCORD_BOT_TOKEN': 'unit-test-token'}), \
                 patch.object(nightly, 'discord_api', side_effect=api):
                nightly.flush_reports()
            self.assertIn('Stopped at: baseline tests (before implementation).', posted['content'])
            self.assertIn('Reason: baseline tests failed.', posted['content'])
            self.assertNotIn('PRIVATE ASSERTION DETAILS', posted['content'])

    def test_unknown_stage_text_is_never_copied_into_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            nightly.progress(run, 'https://private.example/secret')
            nightly.report(run, 'not_deployed', reason='command_failed')
            record = nightly.read_json(run / 'report.json')
            self.assertIsNone(record['stage'])
            self.assertNotIn('private.example', json.dumps(record))

    def test_cannot_weaken_existing_test_or_change_permission_code(self):
        for name in ('tests/test_work_calendar.py', 'jarvis_bot/work_calendar.py', 'jarvis_bot/config.py',
                     'scripts/nightly_improvement.py', '.env', 'compose.yaml', 'requirements.txt'):
            result = self.valid()
            result[name] = 'tampered'
            with self.subTest(name=name), self.assertRaises(nightly.StopRun):
                nightly.check_patch(self.baseline(), result)

    def test_deletion_and_missing_regression_test_rejected(self):
        result = self.valid()
        del result['README.md']
        with self.assertRaises(nightly.StopRun):
            nightly.check_patch(self.baseline(), result)
        result = {**self.baseline(), 'jarvis_bot/progress.py': 'new'}
        with self.assertRaises(nightly.StopRun):
            nightly.check_patch(self.baseline(), result)

    def test_schedule_helpers_and_tests_are_excluded_even_with_valid_progress_patch(self):
        for name in ('jarvis_bot/schedule_alias.py', 'jarvis_bot/availability.py',
                     'jarvis_bot/calendar_helper.py', 'jarvis_bot/ics_reader.py',
                     'tests/test_huddle_extra.py', 'tests/test_calendar_new.py'):
            result = self.valid()
            result[name] = 'schedule change'
            with self.subTest(name=name), self.assertRaisesRegex(
                    nightly.StopRun, 'schedule_improvements_excluded'):
                nightly.check_patch(self.baseline(), result)

    def test_non_schedule_helpers_still_have_a_valid_path_to_deployment(self):
        result = self.baseline()
        result['jarvis_bot/progress_recovery.py'] = 'new helper'
        result['tests/test_progress_recovery_extra.py'] = 'new regression'
        self.assertEqual(len(nightly.check_patch(self.baseline(), result)), 2)

    def test_inventory_excludes_private_state_and_detects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'jarvis_bot').mkdir()
            (root / 'data').mkdir()
            (root / 'jarvis_bot/a.py').write_text('safe')
            (root / '.env').write_text('private-token')
            (root / 'data/private.sqlite3').write_text('private history')
            self.assertEqual(set(nightly.inventory(root)), {'jarvis_bot/a.py'})
            (root / 'jarvis_bot/link.py').symlink_to(root / '.env')
            with self.assertRaises(nightly.StopRun):
                nightly.inventory(root)

    def test_unknown_and_malformed_quota_never_authorises_run(self):
        for value in ({}, {'rateLimits': {'primary': {'usedPercent': True}}},
                      {'rateLimits': {'primary': {'usedPercent': float('nan')},
                                      'secondary': {'usedPercent': 10}}}):
            with self.assertRaises(nightly.StopRun):
                nightly.remaining(value)
        self.assertEqual(nightly.remaining({'rateLimits': {'primary': {'usedPercent': 12},
                         'secondary': {'usedPercent': 40}}}), (88, 60))

    def test_worker_mounts_no_live_home_data_socket_or_environment(self):
        args = nightly.sandbox_args('image', Path('/clean/source'), name='worker',
                    binary=Path('/host/codex'), auth=Path('/private/auth'), writable=True)
        joined = ' '.join(args)
        for forbidden in ('/var/run/docker.sock', '/run/user/1000/bus',
                          'source=/home/your-user,target=', 'DISCORD_BOT_TOKEN', '--env-file'):
            self.assertNotIn(forbidden, joined)
        self.assertIn('--read-only', args)
        self.assertIn('no-new-privileges', args)
        self.assertIn('ALL', args)
        self.assertIn('--interactive', args)  # docker must deliver the piped prompt

    def test_tests_have_no_network_and_source_is_read_only(self):
        args = nightly.sandbox_args('image', Path('/clean/source'), name='tests')
        self.assertIn('none', args)
        self.assertIn('type=bind,source=/clean/source,target=/workspace,readonly', args)

    def test_worker_mounts_codex_execution_helpers_without_host_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('codex', 'codex-code-mode-host', 'codex-exec-server'):
                (root / name).touch()
            args = nightly.sandbox_args('image', Path('/clean/source'), name='worker',
                         binary=root / 'codex', auth=Path('/private/auth'), writable=True)
            for name in ('codex-code-mode-host', 'codex-exec-server'):
                self.assertIn(f'type=bind,source={root / name},target=/usr/local/bin/{name},readonly', args)
            self.assertNotIn(f'type=bind,source={root},target=', ' '.join(args))

    def test_rollback_preserves_subsequent_human_edits(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'project'
            run = root / 'run'
            project.mkdir()
            (run / 'originals').mkdir(parents=True)
            (project / 'one.py').write_text('nightly')
            (project / 'two.py').write_text('human edit')
            (run / 'originals/one.py').write_text('old one')
            (run / 'originals/two.py').write_text('old two')
            state = {'run': str(run), 'changed': ['one.py', 'two.py'],
                     'before': {'one.py': 'old', 'two.py': 'old'},
                     'after': {n: hashlib.sha256(b'nightly').hexdigest() for n in ('one.py', 'two.py')}}
            with patch.object(nightly, 'PROJECT', project):
                self.assertTrue(nightly.restore_source(state))
            self.assertEqual((project / 'one.py').read_text(), 'old one')
            self.assertEqual((project / 'two.py').read_text(), 'human edit')

    def test_report_redacts_long_tokens_links_and_mentions(self):
        text = nightly.safe_text('hello @everyone https://secret.example/a ' + 'a' * 60)
        self.assertNotIn('@everyone', text)
        self.assertNotIn('https://', text)
        self.assertNotIn('a' * 60, text)

    def test_golden_checks_actual_files_and_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'private/data'
            data.mkdir(parents=True)
            database = data / 'test.sqlite3'
            with closing(sqlite3.connect(database)) as db:
                db.execute('create table test (value text)')
            manifest = {'sqlite_integrity': 'ok', 'databases': ['test.sqlite3'],
                        'files': {'private/data/test.sqlite3': {
                            'bytes': database.stat().st_size,
                            'sha256': hashlib.sha256(database.read_bytes()).hexdigest()}}}
            (root / 'manifest.json').write_text(json.dumps(manifest))
            nightly.verify_golden(root)
            database.write_bytes(b'broken backup')
            with self.assertRaises(nightly.StopRun):
                nightly.verify_golden(root)

    def test_restart_accepts_systemd_diagnostics_before_confirmation(self):
        with patch.object(nightly, 'command', return_value='Running as unit: jarvis-restart.service\nQueued jarvis-restart.service; waiting.'):
            nightly.queue_restart()
        with patch.object(nightly, 'command', return_value='No restart was queued'):
            with self.assertRaises(nightly.StopRun):
                nightly.queue_restart()

    def test_build_uses_verified_local_tag_and_exact_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / '20261103T184425Z'
            (run / 'workspace').mkdir(parents=True)
            (run / 'workspace/main.py').write_text('production')
            with patch.object(nightly, 'docker', side_effect=['sha256:old', '', 'sha256:new']):
                self.assertEqual(nightly.build_candidate(run, 'sha256:old', {'main.py': 'hash'}), 'sha256:new')
            recipe = (run / 'build/Dockerfile').read_text()
            self.assertTrue(recipe.startswith('FROM jarvis-discord:nightly-before-20261103t184425z\n'))
            self.assertNotIn('FROM sha256:', recipe)
            self.assertEqual((run / 'build/src/main.py').read_text(), 'production')

    def test_build_rejects_retagged_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(nightly, 'docker', return_value='wrong-image'):
                with self.assertRaises(nightly.StopRun):
                    nightly.build_candidate(Path(tmp), 'expected-image', {})

    def test_outbox_recovers_stale_message_after_status_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'runs/20261103T184425Z'
            nightly.report(run, 'deployed', title='Test improvement')
            record = nightly.read_json(run / 'report.json')
            record['message_id'] = 'old-failure'
            nightly.write_json(run / 'report.json', record)
            posted = {}
            def api(path, token, payload=None):
                if path.endswith('/messages/old-failure'):
                    return {'content': 'Earlier failure', 'channel_id': nightly.CHANNEL}
                if payload is not None:
                    posted.update(payload)
                    return {'id': 'new-success'}
                if path.endswith('/messages/new-success'):
                    return {'content': posted['content'], 'channel_id': nightly.CHANNEL}
                return []
            with patch.object(nightly, 'ROOT', root), \
                 patch('dotenv.dotenv_values', return_value={'DISCORD_BOT_TOKEN': 'unit-test-token'}), \
                 patch.object(nightly, 'discord_api', side_effect=api):
                nightly.flush_reports()
                self.assertNotIn('message_id', nightly.read_json(run / 'report.json'))
                nightly.flush_reports()
            final = nightly.read_json(run / 'report.json')
            self.assertTrue(final['sent'])
            self.assertEqual(final['message_id'], 'new-success')

    def test_outbox_uses_different_nonces_for_different_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'runs/20261103T184425Z'
            payloads = []
            def api(path, token, payload=None):
                if payload is not None:
                    payloads.append(payload)
                    return {'id': str(len(payloads))}
                if '?limit=' in path:
                    return []
                return {'content': payloads[-1]['content'], 'channel_id': nightly.CHANNEL}
            with patch.object(nightly, 'ROOT', root), \
                 patch('dotenv.dotenv_values', return_value={'DISCORD_BOT_TOKEN': 'unit-test-token'}), \
                 patch.object(nightly, 'discord_api', side_effect=api):
                nightly.report(run, 'not_deployed', reason='worker_failed')
                nightly.flush_reports()
                nightly.report(run, 'deployed', title='Recovered improvement')
                nightly.flush_reports()
            self.assertEqual(len(payloads), 2)
            self.assertNotEqual(payloads[0]['nonce'], payloads[1]['nonce'])

    def deployment(self, root):
        run = root / 'runs/example'
        run.mkdir(parents=True)
        state = {'phase': 'queued', 'run': str(run), 'candidate': 'new-image',
                 'previous': 'old-image', 'queued_at': 990,
                 'changed': [], 'before': {}, 'after': {},
                 'result': {'title': 'Useful fix', 'summary': 'Result', 'evidence': 'Regression passed'}}
        nightly.write_json(root / 'deployment.json', state)
        return state, run

    def test_deployment_needs_sustained_health_before_success_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, run = self.deployment(root)
            with patch.object(nightly, 'ROOT', root), \
                 patch.object(nightly, 'container_state', return_value=('new-image', 'healthy')), \
                 patch.object(nightly, 'flush_reports'), patch.object(nightly, 'command'), \
                 patch.object(nightly.time, 'time', side_effect=[1000, 1030, 1061]):
                nightly.supervise()
                self.assertFalse((run / 'report.json').exists())
                nightly.supervise()
                self.assertFalse((run / 'report.json').exists())
                nightly.supervise()
            self.assertEqual(nightly.read_json(run / 'report.json')['status'], 'deployed')

    def test_unhealthy_candidate_rolls_back_and_reports_only_after_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, run = self.deployment(root)
            with patch.object(nightly, 'ROOT', root), \
                 patch.object(nightly, 'container_state', side_effect=[('new-image', 'unhealthy'), ('old-image', 'healthy')]), \
                 patch.object(nightly, 'flush_reports'), patch.object(nightly, 'docker') as docker, \
                 patch.object(nightly, 'restore_source', return_value=False), \
                 patch.object(nightly, 'queue_restart') as restart, \
                 patch.object(nightly.time, 'time', side_effect=[1000, 1060]):
                nightly.supervise()
                self.assertEqual(nightly.read_json(root / 'deployment.json')['phase'], 'rolling_back')
                self.assertFalse((run / 'report.json').exists())
                docker.assert_called_with('image', 'tag', 'old-image', 'jarvis-discord:local')
                restart.assert_called_once()
                nightly.supervise()
            self.assertEqual(nightly.read_json(run / 'report.json')['status'], 'rolled_back')

    def test_interrupted_rollback_retries_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, _ = self.deployment(root)
            state['phase'] = 'rolling_back'
            nightly.write_json(root / 'deployment.json', state)
            with patch.object(nightly, 'ROOT', root), \
                 patch.object(nightly, 'container_state', return_value=('new-image', 'unhealthy')), \
                 patch.object(nightly, 'flush_reports'), patch.object(nightly, 'docker'), \
                 patch.object(nightly, 'restore_source', return_value=False), \
                 patch.object(nightly, 'queue_restart') as restart, \
                 patch.object(nightly.time, 'time', return_value=1000):
                nightly.supervise()
            restart.assert_called_once()

    def test_interrupted_publication_restores_before_reporting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, run = self.deployment(root)
            state['phase'] = 'prepared'
            nightly.write_json(root / 'deployment.json', state)
            with patch.object(nightly, 'ROOT', root), \
                 patch.object(nightly, 'container_state', return_value=('old-image', 'healthy')), \
                 patch.object(nightly, 'flush_reports'), patch.object(nightly, 'docker'), \
                 patch.object(nightly, 'restore_source') as restore, \
                 patch.object(nightly.time, 'time', return_value=1000):
                nightly.supervise()
            restore.assert_called_once()
            self.assertEqual(nightly.read_json(run / 'report.json')['status'], 'implemented_not_deployed')


if __name__ == '__main__':
    unittest.main()
