import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('qa_runner', Path(__file__).with_name('run.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_failure_retries_only_failed_and_keeps_previous_success(self):
        steps = [{'id': name, 'group': 'local', 'kind': 'local', 'command': [sys.executable]}
                 for name in ['good', 'bad']]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.object(runner, 'run_step', side_effect=[{'id': 'good', 'status': 'passed'},
                                                              {'id': 'bad', 'status': 'failed'}]):
                first = runner.execute(steps, directory)
            with patch.object(runner, 'run_step', return_value={'id': 'bad', 'status': 'passed'}) as run:
                second = runner.execute(steps, directory, previous=first)
            self.assertTrue(second['success'])
            self.assertEqual(second['reused'], 1)
            self.assertEqual(run.call_args.args[0]['id'], 'bad')
            self.assertEqual(json.loads((directory / 'summary.json').read_text())['passed'], 2)

    def test_server_mutation_and_publication_are_blocked_by_default(self):
        steps = [{'id': kind, 'kind': kind, 'group': 'checks', 'command': [sys.executable]}
                 for kind in ['server', 'mutating', 'publish']]
        with tempfile.TemporaryDirectory() as temporary, patch.object(runner, 'run_step') as run:
            result = runner.execute(steps, Path(temporary))
            self.assertEqual(result['blocked'], 3)
            run.assert_not_called()

    def test_publication_does_not_run_after_failed_check(self):
        steps = [{'id': 'check', 'kind': 'local', 'group': 'release', 'command': [sys.executable]},
                 {'id': 'publish', 'kind': 'publish', 'group': 'release', 'command': [sys.executable],
                  'depends_on': ['check']}]
        with tempfile.TemporaryDirectory() as temporary, patch.object(runner, 'run_step',
                return_value={'id': 'check', 'status': 'failed'}) as run:
            result = runner.execute(steps, Path(temporary), approved=['publish'])
            self.assertEqual(result['results'][1]['reason'], 'dependency_failed')
            self.assertEqual(run.call_count, 1)

    def test_changed_plan_cannot_reuse_previous_results(self):
        steps = [{'id': 'check', 'kind': 'local', 'group': 'local', 'command': [sys.executable]}]
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(ValueError):
            runner.execute(steps, Path(temporary), previous={'plan_sha256': 'old'})

    def test_changed_source_or_template_invalidates_previous_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / 'scripts/migration'
            directory.mkdir(parents=True)
            template = directory / 'run.sh.in'
            template.write_text('first')
            with patch.object(runner, 'ROOT', root):
                before = runner.signature([])
                template.write_text('second')
                self.assertNotEqual(before, runner.signature([]))

    def test_logs_stay_in_file_and_timeout_is_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = runner.run_step({'id': 'output', 'command': [sys.executable, '-c',
                                     'print("private-log")']}, directory)
            self.assertEqual(record['status'], 'passed')
            self.assertNotIn('private-log', json.dumps(record))
            self.assertIn('private-log', Path(record['log']).read_text())
            record = runner.run_step({'id': 'timeout', 'timeout': 1, 'command': [sys.executable, '-c',
                                     'import time; time.sleep(10)']}, directory)
            self.assertEqual(record['reason'], 'timeout')


class PublicationTests(unittest.TestCase):
    def test_public_sources_include_tools_and_exclude_private_files(self):
        directory = Path(__file__).resolve().parents[1] / 'release'
        with patch.object(sys, 'path', [str(directory), *sys.path]):
            spec = importlib.util.spec_from_file_location('public_publisher', directory / 'publish.py')
            publisher = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(publisher)
            sources = publisher.public_sources()
        self.assertIn('scripts/qa/run.py', sources)
        self.assertIn('scripts/release/publish.py', sources)
        self.assertIn('docs/functional-requirements.md', sources)
        self.assertFalse(any('private-vpn' in name or '..' in Path(name).parts for name in sources))


if __name__ == '__main__':
    unittest.main()
