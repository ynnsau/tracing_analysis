#!/usr/bin/env python3
"""Timeline publication tests: sparse gaps, integrity, baseline checks, and PNGs."""
import copy
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import plot_execution_timeline as plot
from timeline_data import validate_timeline
import test_plot_results as fixtures


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, empty=False):
        run, folder, entry = fixtures.PlotTests.fixture(self, empty=empty)
        s = json.loads((folder/'summary.json').read_text())
        width = 40000000
        s['options'] = dict(timeline_bin_cycles=width)
        s['timeline'] = dict(schema_version=1, bin_cycles=width,
                             origin_timestamp=None if empty else 123,
                             last_timestamp=None if empty else 123+3*width,
                             last_bin=None if empty else 3,
                             attribution='matched_read_request', storage='sparse_event_bins',
                             empty_bins_are_zero=True, csv='execution_timeline.csv')
        (folder/'summary.json').write_text(json.dumps(s))
        with (folder/'execution_timeline.csv').open('w') as stream:
            writer = csv.writer(stream)
            writer.writerow(['group', 'bin', 'start_offset_cycles', 'end_offset_cycles_exclusive',
                             'reads', 'writes', 'matched_pairs'])
            if not empty:
                for b, r, w, m in [(0, 0, 10, 0), (3, 1, 0, 1)]:
                    for group in plot.GROUPS:
                        writer.writerow([group, b, b*width, (b+1)*width,
                                         r if group != 'channel_1' else 0,
                                         w if group != 'channel_1' else 0,
                                         m if group != 'channel_1' else 0])
        manifest = json.loads((run/'manifest.json').read_text())
        manifest['options'] = dict(timeline_bin_cycles=width)
        (run/'manifest.json').write_text(json.dumps(manifest))
        return run, folder, s

    def test_sparse_gap_series_and_huge_empty_span(self):
        bins = {0: dict(matched_pairs=2), 3: dict(matched_pairs=7)}
        x, y = plot.step_series(bins, 3, 40000000)
        self.assertEqual(x, [0, .1, .3, .4])
        self.assertEqual(y, [2, 0, 7, 0])
        x, y = plot.step_series({(1 << 64)-1: dict(matched_pairs=1)}, (1 << 64)-1, 1)
        self.assertEqual(len(x), 3)
        self.assertEqual(y, [0, 1, 0])
        self.assertEqual(plot.step_series({}, None, 40000000), ([], []))

    def test_validate_sparse_conservation_and_metadata(self):
        _, folder, s = self.fixture()
        data = validate_timeline(folder, s, 40000000)
        self.assertEqual(set(data['combined']), {0, 3})
        self.assertEqual(data['combined'][3]['matched_pairs'], 1)
        for key, value in [('bin_cycles', 1), ('origin_timestamp', 999999999),
                           ('last_bin', 4), ('attribution', 'write'), ('empty_bins_are_zero', False)]:
            bad = copy.deepcopy(s); bad['timeline'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_timeline(folder, bad, 40000000)
        with self.assertRaises(ValueError):
            validate_timeline(folder, s, 1)

    def test_reject_corrupt_duplicate_or_missing_csv(self):
        _, folder, s = self.fixture()
        path = folder/'execution_timeline.csv'
        original = path.read_text()
        lines = original.splitlines()
        for changed in ('\n'.join(lines+[lines[-1]])+'\n',
                        '\n'.join(lines[:-1])+'\n',
                        original.replace('combined,3,120000000,160000000,1,0,1',
                                         'combined,3,120000000,160000000,1,0,0')):
            path.write_text(changed)
            with self.assertRaises(ValueError): validate_timeline(folder, s)
        path.unlink()
        with self.assertRaises(FileNotFoundError): validate_timeline(folder, s)

    def test_publication_baseline_and_no_overwrite(self):
        run, folder, s = self.fixture()
        baseline = self.root/'baseline'; (baseline/'one').mkdir(parents=True)
        (baseline/'one'/'summary.json').write_text(json.dumps(s))
        output = self.root/'published'
        plot.publish(run, output, baseline)
        self.assertTrue((output/'PUBLICATION_COMPLETE').is_file())
        for image in (output/'execution_timeline_overview.png', output/'one'/'execution_timeline.png'):
            self.assertTrue(image.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertEqual((output/'one'/'execution_timeline.csv').read_bytes(),
                         (folder/'execution_timeline.csv').read_bytes())
        provenance = json.loads((output/'provenance.json').read_text())
        self.assertEqual(provenance['baseline_comparison']['verified_trace_ids'], ['one'])
        self.assertIn('no Slurm'.lower(), (output/'README.md').read_text().lower())
        with self.assertRaises(ValueError): plot.publish(run, output)
        bad = copy.deepcopy(s); bad['input']['sha256'] = '0'*64
        (baseline/'one'/'summary.json').write_text(json.dumps(bad))
        rejected = self.root/'rejected'
        with self.assertRaises(ValueError): plot.publish(run, rejected, baseline)
        self.assertFalse(rejected.exists())

    def test_empty_timeline_publication(self):
        run, folder, s = self.fixture(empty=True)
        self.assertEqual(validate_timeline(folder, s), {g: {} for g in plot.GROUPS})
        plot.publish(run, self.root/'empty')
        self.assertTrue((self.root/'empty'/'PUBLICATION_COMPLETE').exists())


if __name__ == '__main__':
    unittest.main()
