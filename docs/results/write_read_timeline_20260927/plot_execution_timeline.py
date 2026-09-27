#!/usr/bin/env python3
"""Publish exact matched-read execution timelines locally from validated CSVs."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

import plot_results as common
from timeline_data import GROUPS, validate_timeline

FREQUENCY = 400000000
COLORS = ('#176f76', '#2877b5', '#b75b2b')


def step_series(bins, last_bin, width, field='matched_pairs'):
    """Represent zero gaps exactly with O(occupied bins) points, not O(duration)."""
    if last_bin is None:
        return [], []
    edges = sorted({0, last_bin+1} | set(bins) | {b+1 for b in bins})
    return ([b*width/FREQUENCY for b in edges],
            [bins.get(b, {}).get(field, 0) for b in edges])


def statistics(trace, data, group):
    s, meta = trace['summary'], trace['summary']['timeline']
    bins = data[group]
    first, last = meta['origin_timestamp'], meta['last_timestamp']
    peak = max((r['matched_pairs'] for r in bins.values()), default=0)
    peak_bin = min((b for b, r in bins.items() if peak and r['matched_pairs'] == peak), default=None)
    return dict(trace_id=trace['entry']['trace_id'], label=trace['label'], group=group,
                bin_seconds=meta['bin_cycles']/FREQUENCY,
                observed_span_seconds=(last-first)/FREQUENCY if first is not None else None,
                total_windows=meta['last_bin']+1 if meta['last_bin'] is not None else 0,
                windows_with_pairs=sum(r['matched_pairs'] > 0 for r in bins.values()),
                matched_pairs=s['groups'][group]['matched_writes'],
                reads=s['groups'][group]['reads'], writes=s['groups'][group]['writes'],
                peak_pairs_per_window=peak,
                first_peak_window_start_seconds=peak_bin*meta['bin_cycles']/FREQUENCY if peak_bin is not None else None,
                collector_dropped_records=s['input']['dropped_records'])


def draw_axis(ax, trace, data, group, color):
    meta = trace['summary']['timeline']
    x, y = step_series(data[group], meta['last_bin'], meta['bin_cycles'])
    width = meta['bin_cycles']/FREQUENCY
    if x:
        ax.step(x, y, where='post', color=color, linewidth=1.35)
        ax.fill_between(x, y, step='post', color=color, alpha=.15)
        end = (meta['last_timestamp']-meta['origin_timestamp']+1)/FREQUENCY
        if end < x[-1]:
            ax.axvspan(end, x[-1], color='#999999', alpha=.20)
        ax.set_xlim(0, x[-1])
    else:
        ax.set_xlim(0, width)
        ax.text(.5, .5, 'No valid accesses', transform=ax.transAxes, ha='center')
    if x and not trace['summary']['groups'][group]['matched_writes']:
        ax.text(.5, .5, 'No matched pairs', transform=ax.transAxes, ha='center')
    ax.set_ylim(bottom=0)
    ax.grid(axis='y', alpha=.25)
    ax.spines[['top', 'right']].set_visible(False)
    ax.ticklabel_format(axis='y', style='sci', scilimits=(0, 4), useMathText=True)
    ax.set_xlabel('Seconds since first valid trace access')
    ax.set_ylabel(f'Matched pairs / {width:g} s window')


def plot_trace(trace, data, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True, constrained_layout=True)
    for ax, group, color in zip(axes, GROUPS, COLORS):
        draw_axis(ax, trace, data, group, color)
        ax.set_title(f'{group.replace("_", " ").title()} · '
                     f'{trace["summary"]["groups"][group]["matched_writes"]:,} matched pairs', loc='left')
    fig.suptitle(f'{trace["label"]} — write → first-read execution timeline\n'
                 'Pairs counted at the read request; gray tail = outside the observed span', fontsize=14)
    dropped = trace['summary']['input']['dropped_records']
    axes[-1].set_xlabel('Seconds since first valid trace access\n'
                        f'64B latest-write matching; collector drops: {dropped:,}. Counts are not response completions.')
    fig.savefig(output, dpi=160)
    plt.close(fig)


def plot_overview(traces, datasets, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    rows = math.ceil(len(traces)/2)
    fig, axes = plt.subplots(rows, 2, figsize=(16, 3.2*rows), squeeze=False, constrained_layout=True)
    for ax, trace, data in zip(axes.flat, traces, datasets):
        draw_axis(ax, trace, data, 'combined', COLORS[0])
        ax.set_title(trace['label'], loc='left')
    for ax in list(axes.flat)[len(traces):]:
        ax.set_visible(False)
    fig.suptitle('Matched write → read pairs during execution\n'
                 'Independent per-trace time origins and axis ranges; not phase-aligned or pooled', fontsize=15)
    fig.savefig(output, dpi=160)
    plt.close(fig)


def validate_baseline(traces, baseline):
    if baseline is None:
        return None
    verified = []
    for trace in traces:
        previous = json.loads((baseline/trace['entry']['trace_id']/'summary.json').read_text())
        current = trace['summary']
        common.require(previous['input']['sha256'] == current['input']['sha256'], 'baseline input digest differs')
        common.require(previous['groups'] == current['groups'], 'baseline matching/distance statistics differ')
        common.require(previous['raw_record_slots'] == current['raw_record_slots']
                       and previous['invalid_record_slots'] == current['invalid_record_slots'], 'baseline record counts differ')
        verified.append(trace['entry']['trace_id'])
    return dict(publication=str(baseline.resolve()), verified_trace_ids=verified,
                check='same raw SHA-256, record counts, and all matching/distance group statistics')


def publish(run, output, baseline=None):
    common.require(not output.exists(), f'output already exists: {output}')
    manifest, traces, coverage = common.collect(run)
    expected_width = manifest.get('options', {}).get('timeline_bin_cycles')
    common.require(isinstance(expected_width, int) and expected_width > 0, 'run does not request timelines')
    datasets = [validate_timeline(t['source'], t['summary'], expected_width) for t in traces]
    comparison = validate_baseline(traces, baseline)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.execution_timeline_', dir=output.parent) as tmp:
        stage = Path(tmp)/'publication'; stage.mkdir()
        common.dump_json(stage/'coverage.json', coverage)
        common.dump_json(stage/'source_manifest.json', manifest)
        rows, hashes = [], {}
        for index, (trace, data) in enumerate(zip(traces, datasets), 1):
            ident, s = trace['entry']['trace_id'], trace['summary']
            folder = stage/ident; folder.mkdir()
            for name in ('summary.json', *common.CSV_FILES, 'execution_timeline.csv'):
                source = trace['source']/name
                shutil.copy2(source, folder/name)
                hashes[str(source.resolve())] = hashlib.sha256((folder/name).read_bytes()).hexdigest()
            plot_trace(trace, data, folder/'execution_timeline.png')
            trace_rows = [statistics(trace, data, g) for g in GROUPS]
            rows.extend(trace_rows)
            common.atomic_text(folder/'timeline_summary.csv', common.csv_text(trace_rows))
            dropped = s['input']['dropped_records']
            common.atomic_text(folder/'README.md', f'# {trace["label"]}: execution timeline\n\n'
                f'![Matched pairs by execution time](execution_timeline.png)\n\n'
                f'Each {expected_width/FREQUENCY:g}-second half-open window counts pairs at the read request. '
                'Time zero is the first valid trace event, shared by both channels. Pending writes survive windows. '
                'Gray shading marks the unobserved tail after the last valid timestamp (one tick included).\n\n'
                f'Collector reports **{dropped:,} dropped records**; their time locations are unknown. '
                'An observed zero is not proof of inactivity if capture dropped events.\n\n'
                '[Timeline counts and read/write context](execution_timeline.csv) · '
                '[Timeline summary](timeline_summary.csv) · [Original statistics and time bounds](summary.json).\n\n'
                'CSV offsets are cycles at 400 MHz. Only occupied event bins are stored; omitted bins '
                'within the recorded span have zero observed events. Counts are not response completions, '
                'service latencies, or the probability a write will be read.\n')
            print(f'[timeline plot {index}/{len(traces)}] {trace["label"]}', flush=True)
        plot_overview(traces, datasets, stage/'execution_timeline_overview.png')
        common.atomic_text(stage/'timeline_summary.csv', common.csv_text(rows))
        lines = ['# Write-to-read execution timelines', '',
                 f'{len(traces)} distinct eligible traces, **{expected_width/FREQUENCY:g}-second windows**. '
                 'These are fresh raw-trace analyses; the prior histogram publication is unchanged.', '',
                 '![Per-trace execution timelines](execution_timeline_overview.png)', '',
                 '## Per-trace results', '',
                 '| Trace / detailed plot | Observed span (s) | Matched pairs | Peak pairs/window | First peak window starts (s) | Collector drops |',
                 '|---|---:|---:|---:|---:|---:|']
        for row in rows:
            if row['group'] != 'combined':
                continue
            span = common.shown(row['observed_span_seconds'], 3)
            peak_start = common.shown(row['first_peak_window_start_seconds'], 3)
            lines.append(f'| [{row["label"]}]({row["trace_id"]}/README.md) | {span} | '
                         f'{row["matched_pairs"]:,} | {row["peak_pairs_per_window"]:,} | {peak_start} | '
                         f'{row["collector_dropped_records"]:,} |')
        lines += ['', '## Definitions and limitations', '',
                  '- Time zero is each trace\'s first valid event; x is execution time, not write-to-read distance.',
                  '- A pair is assigned to the read-request window using integer cycle arithmetic. Writes can precede that window.',
                  '- The 64B latest-write/first-read policy is unchanged. Superseded and pending writes are not counted as pairs.',
                  '- CSVs also retain all reads/writes per window. Pair-count peaks may reflect more traffic, not higher reuse probability.',
                  '- Every occupied event bin has all three channel-group rows. Omitted bins are zero; plots preserve gaps.',
                  '- Gray tail shading is outside the observed span; a final partial window is not normalized to a full-window rate.',
                  '- Each overview panel has its own axis ranges. Workload phases are not aligned and timelines are not pooled.',
                  '- Collector drops cannot be localized or corrected from the aggregate header count. These are observed counts.', '',
                  '## Validation and coverage', '',
                  'Each timeline sums to the existing matched/read/write counters, and each bin\'s channel counts sum to combined. '
                  'All prior histogram/provenance checks are also required before publication.', '']
        if comparison:
            lines += [f'All **{len(traces)} traces** have identical raw-file SHA-256 digests and matching/distance '
                      'statistics to the baseline publication. Adding timelines did not change pairing results.', '']
        lines += [f'Coverage: **{coverage["status"]}** across discovered input paths. '
                  f'{coverage["counts"]["invalid_input"]} malformed inputs and '
                  f'{coverage["counts"]["duplicate_alias"]} duplicate aliases remain excluded. '
                  'All eligible distinct traces must finish; this publisher does not silently skip failed tasks.', '',
                  '[Coverage details](coverage.json) · [All timeline summaries](timeline_summary.csv) · '
                  '[Provenance](provenance.json) · [Launch snapshot](source_manifest.json).', '',
                  '## Reproduce locally', '', '```sh', 'python3 scripts/plot_execution_timeline.py \\',
                  '  --run-dir '+shlex.quote(str(run.resolve()))+' \\',
                  '  --output docs/results/execution_timeline_NEW', '```', '',
                  'No Slurm or raw-bin access is needed for plotting. Existing publications are never overwritten. '
                  'The CSVs can be summed into integer multiples of the base window without reparsing; finer windows need a new analysis.']
        common.atomic_text(stage/'README.md', '\n'.join(lines)+'\n')
        scripts = {}
        for name in ('plot_execution_timeline.py', 'plot_results.py', 'timeline_data.py'):
            source = Path(__file__).parent/name
            shutil.copy2(source, stage/name)
            scripts[name] = hashlib.sha256(source.read_bytes()).hexdigest()
        import matplotlib
        common.dump_json(stage/'provenance.json', dict(created_utc=datetime.now(timezone.utc).isoformat(),
                         run_dir=str(run.resolve()), python_version=sys.version,
                         matplotlib_version=matplotlib.__version__, source_sha256=hashes,
                         script_sha256=scripts, bin_cycles=expected_width, baseline_comparison=comparison,
                         manifest_sha256=hashlib.sha256((run/'manifest.json').read_bytes()).hexdigest()))
        common.atomic_text(stage/'PUBLICATION_COMPLETE', f'{len(traces)} complete timelines; coverage={coverage["status"]}\n')
        common.require(not output.exists(), 'output appeared during publication')
        stage.rename(output)
    print(f'Published {len(traces)+1} PNGs and validated timeline data: {output}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-publication', type=Path)
    args = parser.parse_args()
    publish(args.run_dir.resolve(strict=True), args.output.resolve(), args.baseline_publication)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'error: {exc}', file=sys.stderr)
        sys.exit(1)
