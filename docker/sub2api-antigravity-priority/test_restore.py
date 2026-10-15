import pathlib
import tempfile
import unittest
from unittest.mock import patch

import restore


class RecoveryTests(unittest.TestCase):
    def test_versions_reject_future_or_missing_version(self):
        for text in ['Sub2API 0.2.16', 'Sub2API v0.2.16', 'Sub2API 0.2.16-agpriority.1.2.18.sync.1']:
            restore.check_version(text)
        for text in ['Sub2API 0.2.17', 'Sub2API 0.3.0', 'Sub2API dev', '0.2.160', '0.2.16-custom-unknown']:
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                restore.check_version(text)

    def test_compose_preserves_private_references_and_other_services(self):
        original = '''services:
  sub2api:
    image: vendor/app:0.2.16
    environment:
      - ADMIN_PASSWORD=${ADMIN_PASSWORD}
      - ANTIGRAVITY_PRIORITY_ENABLED=false
      - DATABASE_HOST=postgres
    volumes:
      - ./data:/app/data
  postgres:
    image: postgres:18
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
'''
        result = restore.candidate_compose(original, 'sub2api', 'local/restored:test')
        self.assertIn('ADMIN_PASSWORD=${ADMIN_PASSWORD}', result)
        self.assertIn('ANTIGRAVITY_PRIORITY_ENABLED=true', result)
        self.assertNotIn('ANTIGRAVITY_PRIORITY_ENABLED=false', result)
        self.assertEqual(result.split('  postgres:')[1], original.split('  postgres:')[1])
        self.assertIn('      - ./data:/app/data', result)

    def test_compose_mapping_environment_stays_a_mapping(self):
        original = '''services:
  sub2api:
    image: vendor/app:0.2.16
    environment:
      ADMIN_PASSWORD: ${ADMIN_PASSWORD}
      ANTIGRAVITY_PRIORITY_ENABLED: "false"
    restart: unless-stopped
'''
        result = restore.candidate_compose(original, 'sub2api', 'local/restored:test')
        self.assertIn('      ANTIGRAVITY_PRIORITY_ENABLED: "true"', result)
        self.assertIn('      ADMIN_PASSWORD: ${ADMIN_PASSWORD}', result)
        self.assertNotIn('      - ANTIGRAVITY', result)

    def test_compose_without_environment_and_with_following_top_level(self):
        original = 'services:\n  sub2api:\n    image: app:0.2.16\n    volumes:\n      - ./data:/app/data\nvolumes:\n  data:\n'
        result = restore.candidate_compose(original, 'sub2api', 'local/restored:test')
        self.assertIn('    environment:\n', result)
        self.assertTrue(result.endswith('volumes:\n  data:\n'))

    def test_unknown_layout_fails_before_any_write(self):
        for value in ['services:\n sub2api:\n  image: app\n', 'services:\n  sub2api:\n    build: .\n']:
            with self.assertRaises(RuntimeError):
                restore.candidate_compose(value, 'sub2api', 'local/restored:test')

    def test_atomic_write_is_private(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'private.yml'
            restore.atomic(path, b'local-only')
            self.assertEqual(path.read_bytes(), b'local-only')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_commands_do_not_print_private_output(self):
        with patch('restore.subprocess.run') as execute:
            execute.return_value.returncode = 1
            execute.return_value.stderr = b'SYNTHETIC_PRIVATE_VALUE'
            with self.assertRaises(RuntimeError) as failure:
                restore.run(['docker', 'compose'])
            self.assertNotIn('SYNTHETIC_PRIVATE_VALUE', str(failure.exception))

    def test_rollback_refuses_concurrent_config_changes(self):
        import json
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary)
            compose = folder / 'compose.yml'
            compose.write_bytes(b'changed later')
            (folder / 'compose.applied.yml').write_bytes(b'previous candidate')
            (folder / 'rollback.json').write_text(json.dumps({'directory': temporary, 'compose': str(compose)}))
            with patch('restore.run') as execute, self.assertRaises(RuntimeError):
                restore.rollback(folder)
            execute.assert_not_called()
            self.assertEqual(compose.read_bytes(), b'changed later')

    def test_version_uses_actual_first_version(self):
        with self.assertRaises(RuntimeError):
            restore.check_version('Sub2API 0.2.17 based on upstream 0.2.16')

    def test_build_and_image_combination_is_rejected(self):
        with self.assertRaises(RuntimeError):
            restore.candidate_compose('services:\n  sub2api:\n    image: app:0.2.16\n    build: .\n', 'sub2api', 'local/restored:test')

    def test_slow_start_respects_health_configuration(self):
        elapsed = [0]
        def sleep(seconds):
            elapsed[0] += seconds
        def inspect(container):
            return {'Id': container, 'Config': {'Healthcheck': {'StartPeriod': 120000000000, 'Interval': 30000000000, 'Retries': 3}}, 'State': {'Running': True, 'Health': {'Status': 'healthy' if elapsed[0] >= 45 else 'starting'}}}
        with patch('restore.compose', return_value=b'fixture'), patch('restore.inspect_one', side_effect=inspect), patch('restore.time.monotonic', side_effect=lambda: elapsed[0]), patch('restore.time.sleep', side_effect=sleep):
            self.assertEqual(restore.wait_healthy(pathlib.Path('/fixture'), pathlib.Path('/fixture/compose.yml'), 'sub2api')['Id'], 'fixture')
        self.assertEqual(elapsed[0], 45)

    def test_missing_docker_healthcheck_uses_http_health(self):
        item = {'Id': 'fixture', 'Config': {}, 'State': {'Running': True}}
        with patch('restore.compose', return_value=b'fixture'), patch('restore.inspect_one', return_value=item), patch('restore.run', return_value=b'') as execute:
            self.assertEqual(restore.wait_healthy(pathlib.Path('/fixture'), pathlib.Path('/fixture/compose.yml'), 'sub2api'), item)
            self.assertIn('http://127.0.0.1:8080/health', execute.call_args[0][0])


    def test_changed_dependencies_are_rejected_before_stopping_application(self):
        import json
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary)
            file = folder / 'compose.yml'
            text = b'services:\n  sub2api:\n    image: fixture:current\n'
            file.write_bytes(text)
            (folder / 'compose.applied.yml').write_bytes(text)
            (folder / 'compose.before.yml').write_bytes(text)
            (folder / 'rollback.json').write_text(json.dumps({'directory':temporary,'compose':str(file),'service':'sub2api','rollback_image':'fixture:before'}))
            with patch('restore.run', return_value=b''), patch('restore.verify_dependencies', side_effect=RuntimeError('dependency changed')), patch('restore.compose') as execute, self.assertRaises(RuntimeError):
                restore.rollback(folder)
            execute.assert_not_called()
            self.assertEqual(file.read_bytes(),text)

    def test_interrupted_switch_reports_its_matching_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=pathlib.Path(temporary)
            file=directory/'compose.yml'; file.write_bytes(b'applied')
            folder=directory/'priority-restore-backups'/'fixture'; folder.mkdir(parents=True)
            (folder/'compose.applied.yml').write_bytes(b'applied')
            (folder/'rollback.json').write_text('{}')
            self.assertIn('--rollback '+str(folder),restore.interrupted_recovery_hint(directory,file))
            file.write_bytes(b'later user edit')
            self.assertEqual(restore.interrupted_recovery_hint(directory,file),'')

if __name__ == '__main__':
    unittest.main()
