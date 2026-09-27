#!/usr/bin/env python3
"""Independent sequential oracle and end-to-end binary/parallel tests."""
import csv
import hashlib
import json
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
import unittest

BINARY = Path(sys.argv.pop(1)).resolve()
U64_MAX = (1 << 64) - 1
ADDRESS_MASK = (1 << 52) - 1


def event(address, timestamp, write=False, valid=True):
    return ((int(valid) << 63) | (int(write) << 62) | address, timestamp)


def write_cart(path, events, dropped=0):
    with path.open('wb') as out:
        out.write(struct.pack('<IIQQQ', 0x54524143, 1, len(events), len(events), dropped))
        for low, timestamp in events:
            out.write(struct.pack('<QQ', low, timestamp))


def metric(samples):
    bins = [0] * 65
    for v in samples:
        bins[v.bit_length()] += 1
    count = len(samples)
    percentiles = {}
    for p in (50, 90, 95, 99):
        if not count:
            percentiles[f'p{p}'] = None
            continue
        value = sorted(samples)[(count*p+99)//100-1]
        b = value.bit_length()
        percentiles[f'p{p}'] = dict(lower=(1 << (b-1)) if b else 0,
                                    upper=(1 << b)-1 if b else 0)
    return dict(count=count, sum=str(sum(samples)), minimum=min(samples) if count else None,
                maximum=max(samples) if count else None, mean=sum(samples)/count if count else None,
                percentile_intervals=percentiles, histogram_counts=bins)


def reference(events):
    groups = []
    for _ in range(2):
        groups.append(dict(reads=0, writes=0, matched_writes=0, superseded_writes=0,
                           pending_writes_at_end=0, reads_without_pending_write=0,
                           times=[], operations=[]))
    pending = {}
    index = 0
    for low, timestamp in events:
        if not (low >> 63):
            continue
        key = (low & ADDRESS_MASK) >> 6
        g = groups[key & 1]
        if low >> 62 & 1:
            g['writes'] += 1
            if key in pending:
                g['superseded_writes'] += 1
            pending[key] = timestamp, index
        else:
            g['reads'] += 1
            if key in pending:
                write_time, write_index = pending.pop(key)
                g['matched_writes'] += 1
                g['times'].append(timestamp-write_time)
                g['operations'].append(index-write_index-1)
            else:
                g['reads_without_pending_write'] += 1
        index += 1
    for key in pending:
        groups[key & 1]['pending_writes_at_end'] += 1
    combined = {k: groups[0][k]+groups[1][k] for k in groups[0]}
    result = {}
    for name, g in zip(('combined', 'channel_0', 'channel_1'), [combined]+groups):
        g = dict(g)
        times, operations = g.pop('times'), g.pop('operations')
        g.update(valid_accesses=g['reads']+g['writes'],
                 write_match_rate=g['matched_writes']/g['writes'] if g['writes'] else None,
                 zero_time_matches=times.count(0), zero_operation_matches=operations.count(0),
                 time_cycles=metric(times), intervening_operations=metric(operations))
        result[name] = g
    return result


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    def check_equal(self, got, expected):
        if isinstance(expected, dict):
            self.assertEqual(set(got), set(expected))
            for key in expected:
                self.check_equal(got[key], expected[key])
        elif isinstance(expected, float):
            self.assertAlmostEqual(got, expected, delta=max(1, abs(expected))*1e-14)
        else:
            self.assertEqual(got, expected)

    def run_case(self, events, workers=2, block=3, extra=(), dropped=0):
        self.serial += 1
        source = self.root / f'{self.serial}.bin'
        output = self.root / f'result_{self.serial}'
        write_cart(source, events, dropped)
        cmd = [str(BINARY), '--input', str(source), '--output', str(output),
               '--workers', str(workers), '--decode-workers', '3', '--block-records', str(block),
               '--queue-batches', '1', '--prefetch', '5', '--progress-seconds', '0', *extra]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads((output / 'summary.json').read_text())
        limited = '--max-records' in extra
        analyzed = events[:int(extra[extra.index('--max-records')+1])] if limited else events
        self.check_equal(summary['groups'], reference(analyzed))
        self.assertEqual(summary['raw_record_slots'], len(analyzed))
        self.assertEqual(summary['invalid_record_slots'], sum(not (e[0] >> 63) for e in analyzed))
        self.assertEqual(summary['input']['dropped_records'], dropped)
        expected_digest = hashlib.sha256(source.read_bytes()[:32+16*len(analyzed)]).hexdigest()
        self.assertEqual(summary['input']['sha256'], expected_digest)
        self.assertEqual((output / 'COMPLETE').exists(), not limited)
        self.assertEqual((output / 'SMOKE_COMPLETE').exists(), limited)
        self.assertEqual(summary['full_trace'], not limited)
        for filename, metric_key in [('time_histogram.csv', 'time_cycles'),
                                     ('intervening_operations_histogram.csv', 'intervening_operations')]:
            with (output / filename).open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 195)
            for row in rows:
                m = summary['groups'][row['group']][metric_key]
                b = int(row['bin'])
                self.assertEqual(int(row['count']), m['histogram_counts'][b])
                self.assertEqual(int(row['lower_inclusive']), (1 << (b-1)) if b else 0)
                self.assertEqual(int(row['upper_inclusive']), (1 << b)-1 if b else 0)
                if m['count']:
                    self.assertAlmostEqual(float(row['fraction_of_matched_pairs']), int(row['count'])/m['count'])
                else:
                    self.assertEqual(row['fraction_of_matched_pairs'], '')
        again = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(summary, json.loads((output / 'summary.json').read_text()))
        return summary

    def test_hand_computed_pairing_and_global_indices(self):
        events = [event(0, 10, True), event(0, 12, True), event(64, 13, True),
                  event(128, 14, True), event(256, 1000, valid=False), event(0, 20),
                  event(0, 30), event(64, 30), event(128, 31), event(192, 32, True),
                  event(256, 40), event(256, 40, True), event(256, 40),
                  event(512, 40, True), event(512, 40), event(0, 40),
                  event((1 << 48)+128, 41, True), event(128, 42),
                  event((1 << 48)+128+31, 43)]
        summary = self.run_case(events, dropped=123)
        g = summary['groups']['combined']
        self.assertEqual(g['writes'], 8)
        self.assertEqual(g['matched_writes'], 6)
        self.assertEqual(g['superseded_writes'], 1)
        self.assertEqual(g['pending_writes_at_end'], 1)
        # A's pair crosses operations on both derived channels and excludes invalid slot.
        # A has distance 2; line 128 has distance 3, in the same [2,3] bin.
        self.assertEqual(summary['groups']['channel_0']['intervening_operations']['histogram_counts'][2], 2)
        for block in (1, 2, 7, 100):
            self.run_case(events, workers=16, block=block,
                          extra=('--test-delay-first-block-ms', '100'))

    def test_empty_invalid_and_no_match_groups(self):
        for events in ([], [event(0, 9, valid=False)], [event(0, 1)],
                       [event(0, 0, True)], [event(64, 0, True), event(64, 0)]):
            self.run_case(events)

    def test_histogram_u64_boundaries_and_wide_sum(self):
        distances = sorted(set([0, 1, U64_MAX] + [v for b in range(1, 64) for v in ((1 << b)-1, 1 << b)]))
        events = [event(i*64, 0, True) for i in range(len(distances))]
        events += [event(i*64, d) for i, d in enumerate(distances)]
        summary = self.run_case(events, workers=16, block=19)
        self.assertGreater(int(summary['groups']['combined']['time_cycles']['sum']), U64_MAX)
        self.assertEqual(summary['groups']['combined']['time_cycles']['maximum'], U64_MAX)

    def test_randomized_parallel_matches_reference(self):
        rng = random.Random(49722)
        events, timestamp = [], 0
        for _ in range(6000):
            timestamp += rng.randrange(5)
            valid = rng.random() > .12
            address = rng.randrange(300)*64 + rng.randrange(64) + ((1 << 48) if rng.random()<.2 else 0)
            # Reserved bits must not enter the address key.
            low, t = event(address, timestamp if valid else U64_MAX, rng.random()<.56, valid)
            events.append((low | (7 << 55), t))
        for workers in (1, 2, 16):
            for block in (17, 1000):
                self.run_case(events, workers=workers, block=block,
                              extra=('--test-delay-first-block-ms', '100'))

    def test_prefix_is_never_a_full_result(self):
        events = [event(0, 0, True), event(0, 1), event(64, 2, True)]
        for limit in (0, 1, 2, 3, 100):
            self.run_case(events, extra=('--max-records', str(limit)))

    def test_malformed_inputs_and_order_fail_without_complete(self):
        for index, case in enumerate(('short_header', 'bad_magic', 'bad_version', 'size_mismatch',
                                      'count_overflow', 'within_block', 'across_empty_blocks')):
            source = self.root / f'{case}.bin'
            events = [event(0, 2, True), event(0, 1)]
            block = '10'
            if case == 'across_empty_blocks':
                events = [event(0, 2, True), event(64, 900, valid=False),
                          event(64, 1, valid=False), event(0, 1)]
                block = '1'
            write_cart(source, events)
            data = bytearray(source.read_bytes())
            if case == 'short_header': data = data[:8]
            elif case == 'bad_magic': data[0:4] = b'XXXX'
            elif case == 'bad_version': data[4:8] = struct.pack('<I', 99)
            elif case == 'size_mismatch': data += b'0'
            elif case == 'count_overflow': data[16:24] = struct.pack('<Q', U64_MAX)
            source.write_bytes(data)
            output = self.root / f'bad_{index}'
            result = subprocess.run([str(BINARY), '--input', str(source), '--output', str(output),
                                     '--block-records', block, '--queue-batches', '1', '--progress-seconds', '0'],
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0, case)
            self.assertFalse((output / 'COMPLETE').exists(), case)
            self.assertTrue((output / 'FAILED.json').exists(), case)
            if case in ('within_block', 'across_empty_blocks'):
                self.assertIn('timestamps decrease', result.stderr)

    def test_invalid_late_block_cancels_backpressured_workers(self):
        source = self.root / 'late_error.bin'
        # One hot line deliberately routes all traffic to one worker, with a
        # queue depth of one; a late ordering failure must not deadlock drains.
        events = [event(0, i, i % 2 == 0) for i in range(20000)]
        events.append(event(0, 1))
        write_cart(source, events)
        output = self.root / 'late_error'
        result = subprocess.run([str(BINARY), '--input', str(source), '--output', str(output),
                                 '--block-records', '17', '--queue-batches', '1', '--prefetch', '8',
                                 '--progress-seconds', '0'], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('timestamps decrease', result.stderr)
        self.assertFalse((output / 'COMPLETE').exists())

    def test_progress_has_processing_and_memory_counters(self):
        source = self.root / 'progress.bin'
        write_cart(source, [event(0, 0, True), event(0, 10)])
        result = subprocess.run([str(BINARY), '--input', str(source), '--output', str(self.root / 'progress'),
                                 '--test-delay-first-block-ms', '300', '--progress-seconds', '0.05'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('[progress]', result.stderr)
        for field in ('decoded_raw=', 'processed_valid=', 'pending=', 'rss_high_water_MiB=', 'eta_s='):
            self.assertIn(field, result.stderr)


if __name__ == '__main__':
    unittest.main()
