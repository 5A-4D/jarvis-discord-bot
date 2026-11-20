"""Regression coverage for failures before nightly implementation starts."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    'nightly_startup_controller',
    Path(__file__).resolve().parents[1] / 'scripts/nightly_improvement.py')
nightly = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nightly)


class NightlyStartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.install = self.root / 'install'
        self.install.mkdir()
        self.binary = self.root / 'release/bin/codex'
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text('#!/bin/sh\nexit 0\n')
        self.binary.chmod(0o700)
        self.launcher = self.root / 'codex'
        self.launcher.symlink_to(self.binary)
        self.policy = {'codex_binary': str(self.launcher),
                       'minimum_five_hour_remaining': 40,
                       'minimum_week_remaining': 20}
        nightly.write_json(self.install / 'policy.json', self.policy)
        (self.root / 'golden-path').write_text(str(self.root / 'golden'))
        for name, value in [('ROOT', self.root), ('INSTALL', self.install)]:
            patcher = patch.object(nightly, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Fixtures are never executed; /tmp may be mounted noexec in the runner.
        executable = patch.object(nightly.os, 'access',
            side_effect=lambda path, mode: bool(Path(path).stat().st_mode & 0o111))
        executable.start()
        self.addCleanup(executable.stop)

    def failure_report(self, reason):
        with patch.object(nightly, 'flush_reports') as flush, \
             patch.object(nightly, 'read_quota') as quota, \
             patch.object(nightly, 'run_agent') as agent:
            self.assertEqual(nightly.run_nightly(),
                             {'status': 'not_deployed', 'reason': reason})
        flush.assert_called_once()
        quota.assert_not_called()
        agent.assert_not_called()
        self.assertIsNone(nightly.RUN_DEADLINE)
        reports = list((self.root / 'runs').glob('*/report.json'))
        self.assertEqual(len(reports), 1)
        result = nightly.read_json(reports[0])
        self.assertEqual(result['stage'], 'checking_prerequisites')
        self.assertEqual(result['reason'], reason)
        self.assertFalse(result['sent'])
        self.assertFalse((reports[0].parent / 'codex-home').exists())
        return result

    def test_deleted_release_creates_retryable_report_and_preserves_deployment(self):
        deployed = {'phase': 'deployed', 'run': 'previous-run'}
        nightly.write_json(self.root / 'deployment.json', deployed)
        self.binary.unlink()
        self.failure_report('codex_binary_unavailable')
        self.assertEqual(nightly.read_json(self.root / 'deployment.json'), deployed)

    def test_invalid_policy_is_reported_without_private_contents(self):
        (self.install / 'policy.json').write_text('private-value invalid-json')
        result = self.failure_report('maintenance_policy_unavailable')
        self.assertNotIn('private-value', json.dumps(result))

    def test_missing_policy_is_reported(self):
        (self.install / 'policy.json').unlink()
        self.failure_report('maintenance_policy_unavailable')

    def test_startup_failure_outbox_is_delivered_and_receipt_verified(self):
        self.binary.unlink()
        self.failure_report('codex_binary_unavailable')
        posted = {}

        def api(path, token, payload=None):
            if payload is not None:
                posted.update(payload)
                return {'id': 'verified-receipt'}
            if '?limit=' in path:
                return []
            self.assertTrue(path.endswith('/verified-receipt'))
            return {'content': posted['content'], 'channel_id': nightly.CHANNEL}

        with patch('dotenv.dotenv_values', return_value={'DISCORD_BOT_TOKEN': 'test-only'}), \
             patch.object(nightly, 'discord_api', side_effect=api):
            nightly.flush_reports()
        self.assertIn('Stopped at: startup prerequisites.', posted['content'])
        self.assertIn('Reason: codex binary unavailable.', posted['content'])
        self.assertNotIn(str(self.root), posted['content'])
        self.assertEqual(posted['allowed_mentions'], {'parse': []})
        report = next((self.root / 'runs').glob('*/report.json'))
        self.assertTrue(nightly.read_json(report)['sent'])

    def test_missing_backup_pointer_is_reported(self):
        (self.root / 'golden-path').unlink()
        self.failure_report('golden_snapshot_unavailable')

    def test_launcher_tracks_upgrade_and_pins_real_binary_for_each_run(self):
        _, first, _ = nightly.startup_paths()
        second = self.root / 'next-release/bin/codex'
        second.parent.mkdir(parents=True)
        second.write_bytes(self.binary.read_bytes())
        second.chmod(0o700)
        self.launcher.unlink()
        self.launcher.symlink_to(second)
        _, current, _ = nightly.startup_paths()
        self.assertEqual(first, self.binary)
        self.assertEqual(current, second)
        # Each worker can still find its execution helpers beside the real CLI.
        self.assertEqual(current.parent, second.parent)
        self.assertEqual(nightly.read_json(self.install / 'policy.json'), self.policy)

    def test_unexecutable_binary_is_rejected(self):
        self.binary.chmod(0o600)
        self.failure_report('codex_binary_unavailable')

    def test_check_mode_verifies_prerequisites_without_creating_run_or_report(self):
        with patch.object(nightly, 'verify_golden') as verify, \
             patch.object(nightly, 'container_state', return_value=('live', 'healthy')), \
             patch.object(nightly, 'read_quota', return_value=(90, 70)) as quota, \
             patch.object(nightly, 'flush_reports') as flush:
            result = nightly.run_nightly(check_only=True)
        self.assertTrue(result['codex_binary_present'])
        self.assertEqual(result['five_hour_remaining'], 90)
        quota.assert_called_once_with(self.binary)
        verify.assert_called_once_with(self.root / 'golden')
        flush.assert_not_called()
        self.assertFalse((self.root / 'runs').exists())


if __name__ == '__main__':
    unittest.main()
