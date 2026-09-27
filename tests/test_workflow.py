#!/usr/bin/env python3
"""Slurm preparation/task tests; submissions use a fake sbatch, never the cluster."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

BINARY = Path(sys.argv.pop(1)).resolve()
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/workflow.py'


def cart(path, records):
    with path.open('wb') as f:
        f.write(struct.pack('<IIQQQ', 0x54524143, 1, len(records), len(records), 0))
        for address, timestamp, write in records:
            f.write(struct.pack('<QQ', (1 << 63) | (int(write) << 62) | address, timestamp))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.run = self.root / 'run'
        cart(self.inputs / 'a.bin', [(0, 0, True), (0, 5, False)])
        shutil.copy2(self.inputs / 'a.bin', self.inputs / 'copy.bin')
        os.link(self.inputs / 'copy.bin', self.inputs / 'hardlink.bin')
        cart(self.inputs / 'b.bin', [(64, 0, True)])
        (self.inputs / 'invalid.bin').write_bytes(b'short')

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args, success=True, env=None):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                capture_output=True, text=True, timeout=30, env=env)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def prepare(self):
        self.call('prepare', '--run-dir', self.run, '--roots', self.inputs,
                  '--binary', BINARY, '--workers', 2, '--decode-workers', 2,
                  '--cpus-per-task', 6, '--block-records', 1)
        return json.loads((self.run / 'manifest.json').read_text())

    def test_inventory_snapshot_tasks_and_fake_submit(self):
        m = self.prepare()
        self.assertEqual(m['task_count'], 2)
        self.assertEqual(m['options']['timeline_bin_cycles'], 40000000)
        self.assertEqual(m['coverage'], 'partial')
        aliases = [e for e in m['inputs'] if e['status'] == 'duplicate_alias']
        self.assertEqual(len(aliases), 2)
        self.assertEqual(len({e['alias_of'] for e in aliases}), 1)
        self.assertEqual(len([e for e in m['inputs'] if e['status'] == 'invalid_input']), 1)
        self.assertTrue((self.run / 'snapshot/src/write_read_analyzer.cpp').exists())
        for index in (0, 1):
            self.call('task', '--run-dir', self.run, '--index', index)
        statuses = list((self.run / 'status').glob('*.json'))
        self.assertEqual(len(statuses), 2)
        for status in statuses:
            self.assertEqual(json.loads(status.read_text())['status'], 'analyzed')
        for folder in (self.run/'results').iterdir():
            self.assertTrue((folder/'execution_timeline.csv').is_file())
            self.assertEqual(json.loads((folder/'summary.json').read_text())['timeline']['bin_cycles'], 40000000)
        before = {p: p.read_bytes() for p in statuses}
        self.call('task', '--run-dir', self.run, '--index', 0, success=False)
        self.assertEqual(before, {p: p.read_bytes() for p in statuses})
        fakebin = self.root / 'fakebin'
        fakebin.mkdir()
        fake = fakebin / 'sbatch'
        captured = self.root / 'captured.json'
        fake.write_text('#!/usr/bin/env python3\nimport json,sys\n'
                        f'with open({str(captured)!r}, "w") as f: json.dump(sys.argv[1:], f)\n'
                        'print("98765;fake-cluster")\n')
        fake.chmod(0o755)
        env = dict(os.environ, PATH=str(fakebin)+os.pathsep+os.environ['PATH'])
        self.call('submit', '--run-dir', self.run, env=env)
        submission = json.loads((self.run / 'submission.json').read_text())
        self.assertEqual(submission['job_id'], '98765')
        cmd = json.loads(captured.read_text())
        self.assertIn('--array=0-1%2', cmd)
        self.assertIn('--cpus-per-task=6', cmd)
        self.assertIn('--mem=64G', cmd)
        self.call('submit', '--run-dir', self.run, env=env, success=False)
        self.call('prepare', '--run-dir', self.run, '--roots', self.inputs,
                  '--binary', BINARY, success=False)

    def test_changed_source_fails_before_analysis(self):
        m = self.prepare()
        entry = next(e for e in m['inputs'] if e.get('task_index') == 0)
        with Path(entry['path']).open('ab') as f:
            f.write(b'changed')
        self.call('task', '--run-dir', self.run, '--index', 0, success=False)
        status = json.loads((self.run / 'status' / (entry['trace_id']+'.json')).read_text())
        self.assertEqual(status['status'], 'failed_task')
        self.assertFalse((self.run / 'results' / entry['trace_id'] / 'COMPLETE').exists())

    def test_cross_node_device_and_inode_numbers_are_not_compared(self):
        m = self.prepare()
        m['provenance']['host'] = 'another-preparation-node.invalid'
        entry = next(e for e in m['inputs'] if e.get('task_index') == 0)
        entry['identity']['device'] += 123
        entry['identity']['inode'] += 456
        (self.run / 'manifest.json').write_text(json.dumps(m))
        self.call('task', '--run-dir', self.run, '--index', 0)
        status = json.loads((self.run / 'status' / (entry['trace_id']+'.json')).read_text())
        self.assertEqual(status['status'], 'analyzed')
        self.assertNotEqual(status['execution_input_identity']['device'], entry['identity']['device'])

    def test_same_node_identity_change_is_still_rejected(self):
        m = self.prepare()
        entry = next(e for e in m['inputs'] if e.get('task_index') == 0)
        entry['identity']['inode'] += 1
        (self.run / 'manifest.json').write_text(json.dumps(m))
        result = self.call('task', '--run-dir', self.run, '--index', 0, success=False)
        self.assertIn('inode', result.stderr)
        self.assertFalse((self.run / 'results' / entry['trace_id'] / 'COMPLETE').exists())

    def test_cross_node_timestamp_and_header_changes_are_still_rejected(self):
        m = self.prepare()
        m['provenance']['host'] = 'another-preparation-node.invalid'
        entry = next(e for e in m['inputs'] if e.get('task_index') == 0)
        entry['identity']['mtime_ns'] -= 1
        entry['header']['dropped_records'] += 1
        (self.run / 'manifest.json').write_text(json.dumps(m))
        result = self.call('task', '--run-dir', self.run, '--index', 0, success=False)
        self.assertIn('mtime_ns', result.stderr)
        self.assertIn('header', result.stderr)

    def test_failed_only_retry_reuses_success_without_changing_old_run(self):
        m = self.prepare()
        self.call('task', '--run-dir', self.run, '--index', 0)
        self.call('step-failed', '--run-dir', self.run, '--index', 1, '--exit-code', 1)
        before = {str(p.relative_to(self.run)): p.read_bytes()
                  for p in self.run.rglob('*') if p.is_file()}
        retry = self.root / 'retry'
        self.call('retry', '--from-run', self.run, '--run-dir', retry, '--binary', BINARY)
        new = json.loads((retry / 'manifest.json').read_text())
        self.assertEqual(new['task_count'], 1)
        self.assertEqual(new['resources']['max_parallel'], 1)
        self.assertEqual(new['reused_result_count'], 1)
        reused = next(e for e in new['inputs'] if e['status'] == 'reused_result')
        self.assertNotIn('task_index', reused)
        self.assertEqual(Path(reused['result']), self.run / 'results' / reused['trace_id'])
        failed = next(e for e in new['inputs'] if e['status'] == 'eligible')
        self.assertEqual(failed['parent_task_index'], 1)
        self.assertEqual(failed['task_index'], 0)
        self.call('task', '--run-dir', retry, '--index', 0)
        self.assertTrue((retry / 'results' / failed['trace_id'] / 'COMPLETE').exists())
        self.assertEqual(before, {str(p.relative_to(self.run)): p.read_bytes()
                                  for p in self.run.rglob('*') if p.is_file()})
        self.assertEqual(len(m['inputs']), len(new['inputs']))

    def test_retry_refuses_nonterminal_tasks_and_incomplete_success(self):
        m = self.prepare()
        self.call('retry', '--from-run', self.run, '--run-dir', self.root / 'missing',
                  '--binary', BINARY, success=False)
        self.call('task', '--run-dir', self.run, '--index', 0)
        self.call('step-failed', '--run-dir', self.run, '--index', 1, '--exit-code', 1)
        entry = next(e for e in m['inputs'] if e.get('task_index') == 0)
        # Remove a temporary fixture marker, not a real project result.
        (self.run / 'results' / entry['trace_id'] / 'COMPLETE').unlink()
        self.call('retry', '--from-run', self.run, '--run-dir', self.root / 'incomplete',
                  '--binary', BINARY, success=False)

    def test_retry_refuses_success_missing_required_timeline(self):
        m = self.prepare()
        self.call('task', '--run-dir', self.run, '--index', 0)
        self.call('step-failed', '--run-dir', self.run, '--index', 1, '--exit-code', 1)
        entry = next(e for e in m['inputs'] if e.get('task_index') == 0)
        (self.run/'results'/entry['trace_id']/'execution_timeline.csv').unlink()
        self.call('retry', '--from-run', self.run, '--run-dir', self.root/'missing_timeline',
                  '--binary', BINARY, success=False)

    def test_decreasing_timestamps_are_failed_task(self):
        cart(self.inputs / 'a.bin', [(0, 20, True), (0, 10, False)])
        m = self.prepare()
        entry = next(e for e in m['inputs'] if e['dataset_relative_path'] == 'a.bin')
        self.call('task', '--run-dir', self.run, '--index', entry['task_index'], success=False)
        status = json.loads((self.run / 'status' / (entry['trace_id']+'.json')).read_text())
        self.assertEqual(status['status'], 'failed_task')
        self.assertFalse((self.run / 'results' / entry['trace_id'] / 'COMPLETE').exists())


    def test_batch_failure_fallback_preserves_terminal_status(self):
        m = self.prepare()
        entry = next(e for e in m['inputs'] if e.get('task_index') == 0)
        self.call('step-failed', '--run-dir', self.run, '--index', 0, '--exit-code', 137)
        path = self.run / 'status' / (entry['trace_id']+'.json')
        self.assertEqual(json.loads(path.read_text())['status'], 'failed_task')
        before = path.read_bytes()
        self.call('step-failed', '--run-dir', self.run, '--index', 0, '--exit-code', 1)
        self.assertEqual(before, path.read_bytes())


if __name__ == '__main__':
    unittest.main()
