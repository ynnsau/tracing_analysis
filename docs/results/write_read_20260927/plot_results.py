#!/usr/bin/env python3
"""Publish validated write/read histograms locally, without Slurm or raw-bin reads."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile

GROUPS = ('combined', 'channel_0', 'channel_1')
METRICS = ('time_cycles', 'intervening_operations')
COUNTERS = ('valid_accesses', 'reads', 'writes', 'matched_writes', 'superseded_writes',
            'pending_writes_at_end', 'reads_without_pending_write',
            'zero_time_matches', 'zero_operation_matches')
CSV_FILES = ('summary.csv', 'time_histogram.csv', 'intervening_operations_histogram.csv')
PERCENTILES = (50, 90, 95, 99)
U64_MAX = (1 << 64) - 1


def require(ok, message):
    if not ok:
        raise ValueError(message)


def integer(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def bounds(bin_index):
    return (1 << (bin_index-1), (1 << bin_index)-1) if bin_index else (0, 0)


def metric_from_parts(bins, total, minimum, maximum):
    count = sum(bins)
    intervals = {}
    for p in PERCENTILES:
        interval = None
        if count:
            rank, cumulative = (count*p+99)//100, 0
            for i, n in enumerate(bins):
                cumulative += n
                if cumulative >= rank:
                    lo, hi = bounds(i)
                    interval = dict(lower=lo, upper=hi)
                    break
        intervals[f'p{p}'] = interval
    return dict(count=count, sum=str(total), minimum=minimum if count else None,
                maximum=maximum if count else None, mean=total/count if count else None,
                percentile_intervals=intervals, histogram_counts=bins)


def validate_metric(m, count):
    bins = m['histogram_counts']
    require(len(bins) == 65 and all(integer(x) for x in bins), 'invalid histogram bins')
    require(integer(m['count']) and m['count'] == sum(bins) == count, 'histogram count mismatch')
    require(isinstance(m['sum'], str) and re.fullmatch(r'[0-9]+', m['sum']), 'invalid exact sum')
    total = int(m['sum'])
    if count:
        require(integer(m['minimum']) and integer(m['maximum'])
                and m['minimum'] <= m['maximum'] <= U64_MAX, 'invalid min/max')
        occupied = [i for i, n in enumerate(bins) if n]
        require(m['minimum'].bit_length() == occupied[0]
                and m['maximum'].bit_length() == occupied[-1], 'min/max inconsistent with bins')
        require(sum(bounds(i)[0]*n for i, n in enumerate(bins)) <= total
                <= sum(bounds(i)[1]*n for i, n in enumerate(bins)), 'sum inconsistent with bins')
        require(count*m['minimum'] <= total <= count*m['maximum'], 'sum inconsistent with min/max')
    else:
        require(total == 0 and m['minimum'] is None and m['maximum'] is None, 'invalid empty metric')
    expected = metric_from_parts(bins, total, m['minimum'], m['maximum'])
    require(expected['percentile_intervals'] == m['percentile_intervals'], 'percentile interval mismatch')
    require((expected['mean'] is None and m['mean'] is None)
            or (expected['mean'] is not None and math.isclose(expected['mean'], m['mean'], rel_tol=1e-12)),
            'mean mismatch')


def validate_groups(groups):
    require(set(groups) == set(GROUPS), 'missing/unexpected channel groups')
    for g in groups.values():
        require(all(integer(g[k]) for k in COUNTERS), 'invalid counter')
        require(g['valid_accesses'] == g['reads']+g['writes'], 'valid counter mismatch')
        require(g['writes'] == g['matched_writes']+g['superseded_writes']+g['pending_writes_at_end'],
                'write conservation failure')
        require(g['reads'] == g['matched_writes']+g['reads_without_pending_write'], 'read conservation failure')
        expected = g['matched_writes']/g['writes'] if g['writes'] else None
        require((expected is None and g['write_match_rate'] is None)
                or (expected is not None and math.isclose(expected, g['write_match_rate'], rel_tol=1e-12)),
                'write match-rate mismatch')
        for metric in METRICS:
            validate_metric(g[metric], g['matched_writes'])
        require(g['zero_time_matches'] == g['time_cycles']['histogram_counts'][0], 'zero-time mismatch')
        require(g['zero_operation_matches'] == g['intervening_operations']['histogram_counts'][0],
                'zero-operation mismatch')
    combined, ch0, ch1 = (groups[k] for k in GROUPS)
    require(all(combined[k] == ch0[k]+ch1[k] for k in COUNTERS), 'channel counter mismatch')
    for metric in METRICS:
        require(combined[metric]['histogram_counts'] == [a+b for a, b in zip(
            ch0[metric]['histogram_counts'], ch1[metric]['histogram_counts'])], 'channel histogram mismatch')
        require(int(combined[metric]['sum']) == int(ch0[metric]['sum'])+int(ch1[metric]['sum']),
                'channel exact sum mismatch')
        populated = [g[metric] for g in (ch0, ch1) if g[metric]['count']]
        require(combined[metric]['minimum'] == (min(m['minimum'] for m in populated) if populated else None)
                and combined[metric]['maximum'] == (max(m['maximum'] for m in populated) if populated else None),
                'channel min/max mismatch')


def validate_result(folder, entry, expected_digest):
    require((folder/'COMPLETE').is_file(), 'missing COMPLETE marker')
    require(not (folder/'SMOKE_COMPLETE').exists(), 'smoke results cannot be published')
    s = json.loads((folder/'summary.json').read_text())
    require(s['schema_version'] == 1 and s['analysis'] == 'latest_write_first_read_64B', 'unknown result schema')
    require(s['full_trace'] is True and s['timestamp_mhz'] == 400, 'not a full 400-MHz analysis')
    require(s['input']['digest_scope'] == 'full_file', 'not a full-file digest')
    require(expected_digest and s['input']['sha256'] == expected_digest, 'result/status digest mismatch')
    require(s['input']['sha256'] == entry.get('sha256', expected_digest), 'manifest digest mismatch')
    require(s['input']['path'] == entry['path'], 'input path mismatch')
    for key in ('written_records', 'dropped_records', 'buffer_size'):
        require(s['input'][key] == entry['header'][key], f'input header mismatch: {key}')
    require(s['raw_record_slots'] == entry['header']['written_records'], 'incomplete raw coverage')
    require(integer(s['invalid_record_slots']) and s['raw_record_slots'] ==
            s['groups']['combined']['valid_accesses']+s['invalid_record_slots'], 'raw/valid mismatch')
    validate_groups(s['groups'])
    for filename, metric in zip(CSV_FILES[1:], METRICS):
        with (folder/filename).open() as stream:
            rows = list(csv.DictReader(stream))
        require(len(rows) == 195, f'wrong row count: {filename}')
        seen = set()
        for row in rows:
            group, i = row['group'], int(row['bin'])
            require(group in GROUPS and 0 <= i < 65 and (group, i) not in seen, 'invalid/duplicate CSV bin')
            seen.add((group, i))
            m = s['groups'][group][metric]
            require((int(row['lower_inclusive']), int(row['upper_inclusive'])) == bounds(i), 'CSV bin bound mismatch')
            require(int(row['count']) == m['histogram_counts'][i], 'CSV/JSON histogram mismatch')
            fraction = row['fraction_of_matched_pairs']
            require((not m['count'] and fraction == '') or
                    (m['count'] and math.isclose(float(fraction), int(row['count'])/m['count'], rel_tol=1e-12)),
                    'CSV normalized fraction mismatch')
    with (folder/'summary.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    require(len(rows) == 3 and {row['group'] for row in rows} == set(GROUPS), 'invalid summary CSV groups')
    for row in rows:
        require(all(int(row[k]) == s['groups'][row['group']][k] for k in COUNTERS), 'CSV summary count mismatch')
    return s


def trace_label(entry):
    stem = Path(entry['dataset_relative_path']).stem
    if entry['dataset_relative_path'].startswith('trace_09_07/'):
        return '09-07 / ' + stem.replace('_8g_noTHP', ' 8G / no THP').replace('_8g_withTHP', ' 8G / THP').replace('bfs', 'BFS').replace('cc', 'CC')
    known = {'bc': 'BC', 'bfs': 'BFS', 'cc': 'CC', 'pr': 'PR', 'xs_20t': 'XS 20t',
             'xs_20t_300000p': 'XS 20t / 300000p'}
    return 'Original / ' + known.get(stem, stem)


def collect(run, allow_partial=False):
    manifest = json.loads((run/'manifest.json').read_text())
    traces, coverage, ids, hashes = [], [], set(), {}
    for e in manifest['inputs']:
        ident = e['trace_id']
        require(re.fullmatch(r'[A-Za-z0-9_-]+', ident) and ident not in ids, 'unsafe/duplicate trace ID')
        ids.add(ident)
        record = dict(trace_id=ident, path=e['path'], label=trace_label(e), manifest_status=e['status'])
        if e['status'] in ('duplicate_alias', 'invalid_input'):
            record.update(status=e['status'], reason=e.get('error', 'Duplicate of '+e.get('alias_of', '?')))
            coverage.append(record)
            continue
        require(e['status'] in ('eligible', 'reused_result'), 'unknown manifest status')
        try:
            reused = e['status'] == 'reused_result'
            if reused:
                folder, digest = Path(e['result']), e['sha256']
            else:
                status = json.loads((run/'status'/(ident+'.json')).read_text())
                require(status['status'] == 'analyzed', f'task status: {status["status"]}: {status.get("error", "")}')
                folder, digest = run/'results'/ident, status['sha256']
            s = validate_result(folder, e, digest)
            if digest in hashes:
                record.update(status='duplicate_alias', reason='Result SHA-256 duplicates '+hashes[digest])
                coverage.append(record)
                continue
            hashes[digest] = ident
            record.update(status='analyzed', reused=reused, result=str(folder.resolve()), sha256=digest)
            traces.append(dict(entry=e, label=record['label'], source=folder, summary=s, reused=reused))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            if not allow_partial:
                raise ValueError(f'{ident}: {exc}; refusing incomplete publication (or explicitly use --allow-partial)') from exc
            record.update(status='failed_task', reason=str(exc))
        coverage.append(record)
    require(bool(traces), 'no complete distinct traces to plot')
    counts = {k: sum(e['status'] == k for e in coverage) for k in ('analyzed', 'duplicate_alias', 'invalid_input', 'failed_task')}
    return manifest, traces, dict(status='partial' if counts['invalid_input'] or counts['failed_task'] else 'complete',
                                 counts=counts, inputs=coverage)


def pool(summaries):
    groups = {}
    for key in GROUPS:
        inputs = [s['groups'][key] for s in summaries]
        g = {c: sum(x[c] for x in inputs) for c in COUNTERS}
        g['write_match_rate'] = g['matched_writes']/g['writes'] if g['writes'] else None
        for metric in METRICS:
            values = [x[metric] for x in inputs]
            occupied = [v for v in values if v['count']]
            g[metric] = metric_from_parts([sum(v['histogram_counts'][i] for v in values) for i in range(65)],
                                          sum(int(v['sum']) for v in values),
                                          min(v['minimum'] for v in occupied) if occupied else None,
                                          max(v['maximum'] for v in occupied) if occupied else None)
        groups[key] = g
    validate_groups(groups)
    return dict(schema_version=1, analysis='pooled_latest_write_first_read_64B', timestamp_mhz=400,
                trace_count=len(summaries), raw_record_slots=sum(s['raw_record_slots'] for s in summaries),
                invalid_record_slots=sum(s['invalid_record_slots'] for s in summaries),
                collector_dropped_records=sum(s['input']['dropped_records'] for s in summaries), groups=groups)


def percentage(n, d):
    return 100*n/d if d else None


def shown(v, digits=2):
    if isinstance(v, int) and digits == 0:
        return f'{v:,}'
    return f'{v:,.{digits}f}' if v is not None else 'undefined'


def interval_text(m, percentile='p50'):
    v = m['percentile_intervals'][percentile]
    return f'[{v["lower"]:,}, {v["upper"]:,}]' if v else 'undefined'


def short_fraction(m, exponent):
    # Bins 0..exponent cover exactly [0, 2**exponent - 1].
    return sum(m['histogram_counts'][:exponent+1])/m['count'] if m['count'] else None


def atomic_text(path, data):
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_text(data)
    temporary.replace(path)


def dump_json(path, data):
    atomic_text(path, json.dumps(data, indent=2, allow_nan=False)+'\n')


def csv_text(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def flat_row(ident, label, group, s):
    g = s['groups'][group]
    row = dict(trace_id=ident, trace=label, group=group, **{k: g[k] for k in COUNTERS},
               write_match_rate=g['write_match_rate'], superseded_fraction=g['superseded_writes']/g['writes'] if g['writes'] else None,
               pending_fraction=g['pending_writes_at_end']/g['writes'] if g['writes'] else None)
    for metric in METRICS:
        v = g[metric]
        row.update({metric+'_'+k: v[k] for k in ('count', 'sum', 'minimum', 'maximum', 'mean')})
        for p in PERCENTILES:
            interval = v['percentile_intervals'][f'p{p}']
            for edge in ('lower', 'upper'):
                row[f'{metric}_p{p}_{edge}'] = interval[edge] if interval else None
        for exponent in (10, 20):
            row[f'{metric}_fraction_lt_2p{exponent}'] = short_fraction(v, exponent)
    return row


def histogram_csv(summary, metric):
    rows = []
    for group in GROUPS:
        m = summary['groups'][group][metric]
        for i, n in enumerate(m['histogram_counts']):
            lo, hi = bounds(i)
            rows.append(dict(group=group, bin=i, lower_inclusive=lo, upper_inclusive=hi, count=n,
                             fraction_of_matched_pairs=n/m['count'] if m['count'] else None))
    return csv_text(rows)


def pyplot():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10, 'axes.titlesize': 11,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'figure.facecolor': 'white', 'axes.facecolor': 'white', 'savefig.facecolor': 'white'})
    return plt


def bin_label(i):
    if i == 0: return '0'
    if i == 1: return '1'
    if i < 5:
        lo, hi = bounds(i)
        return f'{lo}–{hi}'
    return rf'$[2^{{{i-1}}}, 2^{{{i}}}-1]$'


def plot_histogram(summary, metric, label, output, note):
    plt = pyplot()
    groups = [summary['groups'][g] for g in GROUPS]
    last = max([1]+[i for g in groups for i, n in enumerate(g[metric]['histogram_counts']) if n])
    bins = list(range(last+1))
    percentages = [[100*g[metric]['histogram_counts'][i]/g['matched_writes'] if g['matched_writes'] else 0
                    for i in bins] for g in groups]
    maximum = max(max(v) for v in percentages)
    fig, axes = plt.subplots(1, 3, figsize=(18, 7.2), sharex=True, sharey=True)
    colors = ('#176f76', '#386cb0', '#b96b2d')
    ticks = bins if len(bins) <= 24 else sorted(set([0, 1, last]+list(range(2, last+1, 2))))
    for ax, name, g, values, color in zip(axes, ('Combined', 'Channel 0', 'Channel 1'), groups, percentages, colors):
        ax.bar(bins, values, width=.85, color=color, zorder=3)
        ax.set_xticks(ticks, [bin_label(i) for i in ticks], rotation=75, ha='right', fontsize=8)
        ax.set_xlim(-.8, last+.8)
        ax.set_ylim(0, max(1, min(100, maximum*1.18)))
        ax.grid(axis='y', alpha=.18, zorder=0)
        ax.set_axisbelow(True)
        rate = shown(percentage(g['matched_writes'], g['writes']))+'%' if g['writes'] else 'undefined'
        ax.set_title(f'{name}\n{g["matched_writes"]:,} matched pairs\nWrite match rate: {rate}', pad=13)
        if not g['matched_writes']:
            ax.text(.5, .5, 'No matched pairs', transform=ax.transAxes, ha='center', va='center', color='#555555')
    axes[0].set_ylabel('Matched pairs in this group (%)')
    metric_title = 'Write → first-read time distance' if metric == 'time_cycles' else 'Global intervening-operation distance'
    fig.suptitle(f'{label}\n{metric_title}', fontsize=16, y=.98, fontweight='semibold')
    unit = '400-MHz cycles (1 cycle = 2.5 ns)' if metric == 'time_cycles' else 'Valid read/write accesses across both channels; endpoints excluded'
    fig.text(.5, .12, f'{unit}  ·  categorical power-of-two intervals, including zero', ha='center', fontsize=10)
    fig.text(.5, .052, note, ha='center', fontsize=9, color='#555555')
    fig.text(.5, .025, 'All bins through the last occupied bin are drawn; alternate tick labels may be omitted. Exact bounds/counts are in the CSV.',
             ha='center', fontsize=8, color='#666666')
    fig.subplots_adjust(left=.065, right=.99, top=.72, bottom=.30, wspace=.10)
    fig.savefig(output, dpi=170)
    plt.close(fig)


def plot_outcomes(traces, output):
    plt = pyplot()
    labels = [t['label'] for t in traces]
    fig, ax = plt.subplots(figsize=(13.8, max(5, .49*len(labels)+2.7)))
    positions, left = list(range(len(traces))), [0]*len(traces)
    for field, label, color in (('matched_writes', 'Matched first read', '#176f76'),
                                ('superseded_writes', 'Superseded before read', '#d8a34a'),
                                ('pending_writes_at_end', 'Pending at trace end', '#a5adb6')):
        values = [percentage(t['summary']['groups']['combined'][field], t['summary']['groups']['combined']['writes']) or 0 for t in traces]
        bars = ax.barh(positions, values, left=left, height=.67, label=label, color=color)
        for bar, value in zip(bars, values):
            if value >= 3:
                ax.text(bar.get_x()+bar.get_width()/2, bar.get_y()+bar.get_height()/2,
                        f'{value:.1f}%', ha='center', va='center', fontsize=9,
                        color='white' if field == 'matched_writes' else '#202a35')
        left = [a+b for a, b in zip(left, values)]
    for i, t in enumerate(traces):
        if not t['summary']['groups']['combined']['writes']:
            ax.text(2, i, 'No writes; rates undefined', va='center', color='#666666')
    ax.set_yticks(positions, labels)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xlabel('Percentage of valid writes (each trace normalized separately)')
    ax.grid(axis='x', alpha=.18)
    ax.set_axisbelow(True)
    ax.legend(loc='lower center', bbox_to_anchor=(.5, 1.01), ncol=3, frameon=False)
    fig.suptitle('What happens to each observed 64-byte-line write?', fontsize=16, fontweight='semibold', y=.985)
    fig.text(.5, .024, 'Latest write → first subsequent read. Pending at the observation boundary does not mean never read. See README for drops/exclusions.',
             ha='center', fontsize=9, color='#555555')
    fig.subplots_adjust(left=.265, right=.985, top=.84, bottom=.14)
    fig.savefig(output, dpi=170)
    plt.close(fig)


def group_table(summary):
    lines = ['| Group | Writes | Matched | Match rate | Superseded | Pending at end | Reads without pending write |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for name in GROUPS:
        g = summary['groups'][name]
        lines.append(f'| {name} | {g["writes"]:,} | {g["matched_writes"]:,} | {shown(percentage(g["matched_writes"],g["writes"]))}% '
                     f'| {g["superseded_writes"]:,} | {g["pending_writes_at_end"]:,} | {g["reads_without_pending_write"]:,} |')
    return '\n'.join(lines)


def trace_readme(label, summary, source=None, note=''):
    g = summary['groups']['combined']
    lines = [f'# {label}', '', note, '',
             '![Time distance](time_histogram.png)', '',
             '![Intervening operations](intervening_operations_histogram.png)', '',
             '## Write outcomes', '', group_table(summary), '',
             '## Matched-pair distances (combined)', '',
             '| Metric | Exact minimum | Mean | Exact maximum | P50 interval | P90 interval | P95 interval | P99 interval |',
             '|---|---:|---:|---:|---|---|---|---|']
    for metric in METRICS:
        m = g[metric]
        lines.append(f'| {metric} | {shown(m["minimum"],0)} | {shown(m["mean"],3)} | {shown(m["maximum"],0)} | '+
                     ' | '.join(interval_text(m, f'p{p}') for p in PERCENTILES)+' |')
    lines += ['', 'Percentiles are nearest-rank **bin intervals**, not exact values. Histograms contain matched pairs only; '
              'write match rates include superseded and pending writes in the denominator.', '',
              f'Raw slots: {summary["raw_record_slots"]:,}; valid accesses: {g["valid_accesses"]:,}; '
              f'invalid slots: {summary["invalid_record_slots"]:,}.', '',
              'Data: [summary JSON](summary.json), [summary CSV](summary.csv), '
              '[time bins](time_histogram.csv), [operation bins](intervening_operations_histogram.csv).', '',
              '[Back to results](../README.md).']
    if source:
        lines += ['', 'Original result directory: `'+str(source)+'`.',
                  'Input: `'+summary['input']['path']+'`.',
                  'Input SHA-256: `'+summary['input']['sha256']+'`.']
    return '\n'.join(lines)+'\n'


def main_readme(traces, pooled, coverage, run):
    c = coverage['counts']; g = pooled['groups']['combined']
    lines = ['# Write-to-read analysis results', '',
             f'{c["analyzed"]} distinct full traces analyzed; {g["valid_accesses"]:,} valid accesses. '
             f'Coverage is **{coverage["status"]}** relative to all discovered paths: '
             f'{c["invalid_input"]} invalid inputs, {c["failed_task"]} failed/incomplete results, '
             f'{c["duplicate_alias"]} duplicate aliases excluded.', '',
             'These are 64-byte **latest-write → first-subsequent-read** observations, not cache hits or read latencies. '
             'The source trace timestamps are interpreted as 400-MHz cycles.', '',
             '![Per-trace write outcomes](write_outcomes.png)', '',
             '## Per-trace summary', '',
             'Write fractions use all valid writes. Time and operation means use matched pairs only.', '',
             '| Trace / charts | Matched writes | Match rate | Superseded | Pending at end | Mean time (cycles) | Mean time (s) | Mean intervening operations | Collector drops |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for t in traces:
        v = t['summary']['groups']['combined']; w = v['writes']; mean = v['time_cycles']['mean']
        lines.append(f'| [{t["label"]}]({t["entry"]["trace_id"]}/README.md) | {v["matched_writes"]:,} '
                     f'| {shown(percentage(v["matched_writes"],w))}% | {shown(percentage(v["superseded_writes"],w),3)}% '
                     f'| {shown(percentage(v["pending_writes_at_end"],w))}% | {shown(mean,0)} '
                     f'| {shown(mean/4e8 if mean is not None else None,3)} | {shown(v["intervening_operations"]["mean"],0)} '
                     f'| {t["summary"]["input"]["dropped_records"]:,} |')
    lines += ['', '## Pooled result', '',
              f'{g["matched_writes"]:,} matched pairs from {g["writes"]:,} writes: '
              f'**{shown(percentage(g["matched_writes"],g["writes"]))}% matched**, '
              f'{shown(percentage(g["superseded_writes"],g["writes"]))}% superseded, '
              f'{shown(percentage(g["pending_writes_at_end"],g["writes"]))}% pending at end.', '',
              '![Pooled time distance](pooled/time_histogram.png)', '',
              '![Pooled intervening operations](pooled/intervening_operations_histogram.png)', '',
              '[Pooled tables and percentile intervals](pooled/README.md).', '',
              'Pooling sums integer counters, histogram counts, and exact distance sums before computing rates, means, '
              'or percentile intervals. No per-trace percentages are averaged. Trace boundaries remain independent; '
              'there is no matching across files. Pooled figures reflect this workload mixture, not a universal workload.', '',
              '## Short-distance fractions', '',
              'Each entry is the percentage of **matched pairs**, at exact power-of-two bin boundaries. '
              '`2^10` cycles = 2.56 μs; `2^20` cycles = 2.62144 ms. '
              'Operation thresholds count observed traffic across both channels, not unique lines.', '',
              '| Trace | Time < 2^10 cycles | Time < 2^20 cycles | Operations < 2^10 | Operations < 2^20 |',
              '|---|---:|---:|---:|---:|']
    for label, s in [(t['label'], t['summary']) for t in traces]+[('Pooled', pooled)]:
        v = s['groups']['combined']; values = [short_fraction(v[k], n) for k in METRICS for n in (10,20)]
        lines.append('| '+label+' | '+' | '.join(shown(x*100 if x is not None else None,4)+'%' for x in values)+' |')
    lines += ['', '## Coverage and interpretation limits', '',
              '- Pending writes at end are right-censored; the trace cannot establish whether they are read later.',
              '- Supersession is line-granular; a read could consume bytes from several writes. These are not per-byte dependence counts.',
              '- Collector drops can hide reads, writes, and supersession. Their bias is not necessarily one-directional.',
              '- Histogram percentiles are bin intervals, not exact sample percentiles. Exact minima, maxima, and sums are retained.',
              '- Time distance is elapsed time between recorded accesses, not memory-service latency. Intervening operations are not cache reuse distance.',
              '- New THP/no-THP traces are separate recorded runs. Observed differences alone do not prove THP caused them.',
              f'- {pooled["invalid_record_slots"]:,} invalid slots were skipped; '
              f'{pooled["collector_dropped_records"]:,} collector-dropped accesses were reported across included traces.', '',
              '| Input | Status | Reason |', '|---|---|---|']
    for e in coverage['inputs']:
        if e['status'] != 'analyzed':
            lines.append(f'| `{e["path"]}` | {e["status"]} | {e.get("reason", "").replace("|", "/")} |')
    lines += ['', '## Reproduction and data', '',
              'Run locally from the repository root, using a fresh output directory:', '',
              '```sh', 'python3 scripts/plot_results.py \\',
              '  --run-dir '+shlex.quote(str(run.resolve()))+' \\',
              '  --output docs/results/write_read_new', '```', '',
              'No Slurm jobs or raw-bin reads are needed. Matplotlib renders PNGs using the noninteractive Agg backend. '
              'The publisher validates full-trace markers, metadata/digests, counters, channel totals, CSV bins, and percentile intervals '
              'before rendering. It follows `reused_result` references and excludes duplicates. Failed/incomplete results reject '
              'publication unless `--allow-partial` is explicitly requested. It refuses to overwrite an existing publication.', '',
              '[All trace/channel rows](summary.csv) · [Coverage manifest](coverage.json) · '
              '[Provenance and source hashes](provenance.json) · [Launch manifest snapshot](source_manifest.json) · '
              '[Publisher script snapshot](plot_results.py).', '',
              'Every trace subdirectory contains portable JSON/CSV snapshots, two PNGs, and a README. '
              'The `PUBLICATION_COMPLETE` marker is emitted only when the complete publication succeeds.']
    return '\n'.join(lines)+'\n'


def publish(run, output, allow_partial=False):
    require(not output.exists(), f'output already exists: {output}')
    manifest, traces, coverage = collect(run, allow_partial)
    pooled = pool([t['summary'] for t in traces])
    pooled['coverage'] = coverage['status']
    pooled['trace_ids'] = [t['entry']['trace_id'] for t in traces]
    output.parent.mkdir(parents=True, exist_ok=True)
    # Build beside the destination, so failed plotting never publishes a partial
    # directory. Only this newly created private staging directory is cleaned up.
    with tempfile.TemporaryDirectory(prefix='.write_read_plot_', dir=output.parent) as tmp:
        stage = Path(tmp)/'publication'
        stage.mkdir()
        dump_json(stage/'coverage.json', coverage)
        dump_json(stage/'source_manifest.json', manifest)
        rows, source_hashes = [], {}
        for index, t in enumerate(traces, 1):
            ident, s, source = t['entry']['trace_id'], t['summary'], t['source']
            folder = stage/ident; folder.mkdir()
            for name in ('summary.json', *CSV_FILES):
                shutil.copy2(source/name, folder/name)
                source_hashes[str((source/name).resolve())] = hashlib.sha256((folder/name).read_bytes()).hexdigest()
            dropped = s['input']['dropped_records']
            note = f'64-byte latest-write → first-read pairs only. Collector drops reported: {dropped:,}.'
            for metric, filename in zip(METRICS, ('time_histogram.png','intervening_operations_histogram.png')):
                plot_histogram(s, metric, t['label'], folder/filename, note)
            atomic_text(folder/'README.md', trace_readme(t['label'], s, source, note))
            rows.extend(flat_row(ident, t['label'], group, s) for group in GROUPS)
            print(f'[plot {index}/{len(traces)}] {t["label"]}', flush=True)
        folder = stage/'pooled'; folder.mkdir()
        dump_json(folder/'summary.json', pooled)
        pooled_rows = [flat_row('pooled', 'Pooled', group, pooled) for group in GROUPS]
        rows.extend(pooled_rows)
        atomic_text(folder/'summary.csv', csv_text(pooled_rows))
        for metric, stem in zip(METRICS, ('time_histogram', 'intervening_operations_histogram')):
            atomic_text(folder/(stem+'.csv'), histogram_csv(pooled, metric))
            plot_histogram(pooled, metric, f'Pooled · {len(traces)} distinct traces', folder/(stem+'.png'),
                           f'Coverage: {coverage["status"]}; {coverage["counts"]["invalid_input"]} invalid inputs and '
                           f'{coverage["counts"]["failed_task"]} failed tasks excluded. Duplicate copies are not counted.')
        atomic_text(folder/'README.md', trace_readme('Pooled result', pooled, note='Coverage: '+coverage['status']))
        atomic_text(stage/'summary.csv', csv_text(rows))
        atomic_text(stage/'README.md', main_readme(traces, pooled, coverage, run))
        shutil.copy2(Path(__file__), stage/'plot_results.py')
        plot_outcomes(traces, stage/'write_outcomes.png')
        import matplotlib
        dump_json(stage/'provenance.json', dict(created_utc=datetime.now(timezone.utc).isoformat(),
                  run_dir=str(run.resolve()), matplotlib_version=matplotlib.__version__,
                  python_version=sys.version, script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  manifest_sha256=hashlib.sha256((run/'manifest.json').read_bytes()).hexdigest(),
                  source_sha256=source_hashes, included_trace_ids=pooled['trace_ids'],
                  normalization='histogram percentages per matched group; outcome percentages per all writes',
                  percentile_definition='nearest-rank power-of-two bin interval'))
        atomic_text(stage/'PUBLICATION_COMPLETE', f'{len(traces)} distinct complete traces; coverage={coverage["status"]}\n')
        require(not output.exists(), f'output appeared during publication: {output}')
        stage.rename(output)
    print(f'Published {len(traces)*2+3} PNGs and data/reports: {output}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-partial', action='store_true')
    args = parser.parse_args()
    publish(args.run_dir.resolve(strict=True), args.output.resolve(), args.allow_partial)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'error: {exc}', file=sys.stderr)
        sys.exit(1)
