#!/usr/bin/env python3
"""Publisher regressions: exact pooling, reuse, coverage, integrity, and PNGs."""
import copy
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/plot_results.py'
spec = importlib.util.spec_from_file_location('plot_results', SCRIPT)
plot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plot)


def metric(samples):
    bins = [0]*65
    for value in samples:
        bins[value.bit_length()] += 1
    return plot.metric_from_parts(bins, sum(samples), min(samples) if samples else None,
                                  max(samples) if samples else None)


def group(writes=0, times=(), operations=None):
    if operations is None:
        operations = times
    matched = len(times)
    return dict(valid_accesses=writes+matched, reads=matched, writes=writes,
                matched_writes=matched, superseded_writes=0, pending_writes_at_end=writes-matched,
                reads_without_pending_write=0, zero_time_matches=list(times).count(0),
                zero_operation_matches=list(operations).count(0),
                write_match_rate=matched/writes if writes else None,
                time_cycles=metric(times), intervening_operations=metric(operations))


def summary(writes=10, times=(1,), channel=0):
    g = group(writes, times)
    return dict(schema_version=1, analysis='latest_write_first_read_64B', full_trace=True,
                timestamp_mhz=400, raw_record_slots=g['valid_accesses'], invalid_record_slots=0,
                input=dict(path='/raw/input.bin', digest_scope='full_file', sha256='a'*64,
                           written_records=g['valid_accesses'], dropped_records=0, buffer_size=4096),
                groups=dict(combined=copy.deepcopy(g), channel_0=copy.deepcopy(g) if channel == 0 else group(),
                            channel_1=copy.deepcopy(g) if channel == 1 else group()))


class PlotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, ident='one', reused=False, empty=False):
        run = self.root/'run'
        (run/'status').mkdir(parents=True, exist_ok=True)
        folder = self.root/('source_'+ident) if reused else run/'results'/ident
        folder.mkdir(parents=True)
        s = summary(0, ()) if empty else summary()
        s['input']['path'] = '/raw/'+ident+'.bin'
        s['input']['sha256'] = ('a' if ident == 'one' else 'b')*64
        (folder/'summary.json').write_text(json.dumps(s))
        (folder/'COMPLETE').write_text('complete')
        for filename, name in zip(plot.CSV_FILES[1:], plot.METRICS):
            (folder/filename).write_text(plot.histogram_csv(s, name))
        (folder/'summary.csv').write_text(plot.csv_text([
            dict(group=k, **{c:s['groups'][k][c] for c in plot.COUNTERS}) for k in plot.GROUPS]))
        entry = dict(trace_id=ident, dataset_relative_path=ident+'.bin', path=s['input']['path'],
                     header={k:s['input'][k] for k in ('written_records','dropped_records','buffer_size')},
                     status='reused_result' if reused else 'eligible')
        if reused:
            entry.update(result=str(folder), sha256=s['input']['sha256'])
        else:
            entry['task_index'] = 0
            (run/'status'/(ident+'.json')).write_text(json.dumps(dict(status='analyzed', sha256=s['input']['sha256'])))
        manifest_path = run/'manifest.json'
        m = json.loads(manifest_path.read_text()) if manifest_path.exists() else dict(inputs=[])
        m['inputs'].append(entry)
        manifest_path.write_text(json.dumps(m))
        return run, folder, entry

    def test_weighted_pooling_and_percentile_reconstruction(self):
        a, b = summary(10, [1], 0), summary(100, [8]*90, 1)
        pooled = plot.pool([a,b]); g = pooled['groups']['combined']
        self.assertEqual(g['writes'], 110)
        self.assertAlmostEqual(g['write_match_rate'], 91/110)
        self.assertNotAlmostEqual(g['write_match_rate'], (.1+.9)/2)
        self.assertEqual(g['time_cycles']['sum'], '721')
        self.assertAlmostEqual(g['time_cycles']['mean'], 721/91)
        self.assertEqual(g['time_cycles']['percentile_intervals']['p50'], dict(lower=8,upper=15))
        self.assertEqual(g['time_cycles']['histogram_counts'][4], 90)

    def test_empty_groups_wide_sums_and_boundary_fractions(self):
        pooled = plot.pool([summary(0, ())])
        self.assertIsNone(pooled['groups']['combined']['write_match_rate'])
        self.assertIsNone(pooled['groups']['combined']['time_cycles']['mean'])
        self.assertIsNone(plot.short_fraction(pooled['groups']['combined']['time_cycles'], 10))
        m = metric([plot.U64_MAX]*3)
        plot.validate_metric(m, 3)
        self.assertEqual(m['sum'], str(3*plot.U64_MAX))
        self.assertEqual(plot.shown(plot.U64_MAX, 0), '18,446,744,073,709,551,615')
        self.assertEqual(m['percentile_intervals']['p99'], dict(lower=1<<63,upper=plot.U64_MAX))
        self.assertEqual(plot.short_fraction(metric([0,1,1023,1024,1<<20]), 10), 3/5)
        self.assertEqual(plot.short_fraction(metric([0,1,1023,1024,1<<20]), 20), 4/5)

    def test_result_validation_rejects_corrupt_json_and_csv(self):
        run, folder, e = self.fixture()
        good = json.loads((folder/'summary.json').read_text())
        corrupt = copy.deepcopy(good)
        corrupt['groups']['combined']['time_cycles']['sum'] = '99999'
        (folder/'summary.json').write_text(json.dumps(corrupt))
        with self.assertRaises(ValueError): plot.collect(run)
        (folder/'summary.json').write_text(json.dumps(good))
        lines = (folder/'time_histogram.csv').read_text().splitlines()
        # Duplicating a bin is rejected even when its zero count happens to match.
        lines[2] = lines[1]
        (folder/'time_histogram.csv').write_text('\n'.join(lines)+'\n')
        with self.assertRaises(ValueError): plot.collect(run)

    def test_reused_results_and_excluded_inputs_are_accounted_for(self):
        run, _, _ = self.fixture()
        self.fixture('two', reused=True)
        m = json.loads((run/'manifest.json').read_text())
        m['inputs'] += [dict(trace_id='alias',path='/raw/copy.bin',dataset_relative_path='copy.bin',
                             status='duplicate_alias',alias_of='one'),
                        dict(trace_id='broken',path='/raw/bad.bin',dataset_relative_path='bad.bin',
                             status='invalid_input',error='size mismatch')]
        (run/'manifest.json').write_text(json.dumps(m))
        _, traces, coverage = plot.collect(run)
        self.assertEqual(len(traces), 2)
        self.assertEqual(sum(t['reused'] for t in traces), 1)
        self.assertEqual(coverage['status'], 'partial')
        self.assertEqual(coverage['counts'], dict(analyzed=2,duplicate_alias=1,invalid_input=1,failed_task=0))

    def test_missing_marker_and_smoke_require_exclusion(self):
        run, _, _ = self.fixture()
        _, bad, _ = self.fixture('two')
        (bad/'SMOKE_COMPLETE').write_text('smoke')
        with self.assertRaises(ValueError): plot.collect(run)
        _, traces, coverage = plot.collect(run, allow_partial=True)
        self.assertEqual(len(traces), 1)
        self.assertEqual(coverage['counts']['failed_task'], 1)
        (bad/'SMOKE_COMPLETE').unlink()
        (bad/'COMPLETE').unlink()
        with self.assertRaises(ValueError): plot.collect(run)

    def test_duplicate_result_digests_are_not_pooled_twice(self):
        run, _, _ = self.fixture()
        _, folder, e = self.fixture('two', reused=True)
        s = json.loads((folder/'summary.json').read_text())
        s['input']['sha256'] = 'a'*64
        (folder/'summary.json').write_text(json.dumps(s))
        m = json.loads((run/'manifest.json').read_text())
        m['inputs'][1]['sha256'] = 'a'*64
        (run/'manifest.json').write_text(json.dumps(m))
        _, traces, coverage = plot.collect(run)
        self.assertEqual(len(traces), 1)
        self.assertEqual(coverage['counts']['duplicate_alias'], 1)

    @unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'matplotlib is not installed')
    def test_publication_renders_pngs_and_refuses_overwrite(self):
        run, _, _ = self.fixture(empty=True)
        destination = self.root/'publication'
        plot.publish(run, destination)
        self.assertTrue((destination/'PUBLICATION_COMPLETE').exists())
        images = list(destination.rglob('*.png'))
        self.assertEqual(len(images), 5)
        for image in images:
            self.assertEqual(image.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
        with (destination/'summary.csv').open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 6)
        self.assertIn('undefined', (destination/'one/README.md').read_text())
        before = (destination/'provenance.json').read_bytes()
        with self.assertRaises(ValueError): plot.publish(run, destination)
        self.assertEqual(before, (destination/'provenance.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
