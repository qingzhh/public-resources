import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import priority_mapping


class MappingTests(unittest.TestCase):
    def test_unknown_mapping_values_are_rejected(self):
        for value in ['-1000', '2000', 'not-a-version']:
            def run(args, data):
                return json.dumps({'offset': value}).encode()
            with self.assertRaises(RuntimeError):
                priority_mapping.read_offset(run, 'fixture')
        for target in [-1, 1, 2000, True, '1000']:
            with self.assertRaises(RuntimeError):
                priority_mapping.migration_sql(target)

    def test_marker_required_when_database_already_uses_offset(self):
        with tempfile.TemporaryDirectory() as folder:
            priority_mapping.validate_marker(folder, 0)
            with self.assertRaises(RuntimeError):
                priority_mapping.validate_marker(folder, 1000)
            path = pathlib.Path(folder) / 'priority-mapping.json'
            path.write_text('{"schema_version":1,"native_offset":1000}')
            priority_mapping.validate_marker(folder, 1000)
            with self.assertRaises(RuntimeError):
                priority_mapping.validate_marker(folder, 0)

    def test_failed_transaction_cannot_publish_completed_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'priority-mapping.json'
            def fail(args, data):
                raise RuntimeError('transaction failed')
            with self.assertRaises(RuntimeError):
                priority_mapping.migrate(fail, 'fixture', folder)
            self.assertFalse(path.exists())

    def test_database_commit_precedes_private_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'priority-mapping.json'
            def run(args, data):
                self.assertFalse(path.exists())
                return b'{"previous_offset":0,"target_offset":1000,"changed_accounts":6}\n'
            result = priority_mapping.migrate(run, 'fixture', folder)
            self.assertEqual(result['changed_accounts'], 6)
            priority_mapping.validate_marker(folder, 1000)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_local_marker_write_failure_keeps_database_result_recoverable(self):
        with tempfile.TemporaryDirectory() as folder:
            def run(args, data):
                return b'{"previous_offset":1000,"target_offset":1000,"changed_accounts":0}'
            with patch('priority_mapping.os.replace', side_effect=OSError('injected marker failure')), self.assertRaises(OSError):
                priority_mapping.migrate(run, 'fixture', folder)
            self.assertEqual(list(pathlib.Path(folder).iterdir()), [])
            result = priority_mapping.migrate(run, 'fixture', folder)
            self.assertEqual(result['changed_accounts'], 0)
            priority_mapping.validate_marker(folder, 1000)


if __name__ == '__main__':
    unittest.main()
