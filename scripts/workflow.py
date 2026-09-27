#!/usr/bin/env python3
"""Prepare immutable input/run snapshots and execute one Slurm-array task.

No polling, aggregation, or plotting is started by this program.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS = [Path('/research/yans3/gitdoc/remap_tracing'), Path('/research/yans3/trace')]


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, data):
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w') as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.write('\n')
    temp.replace(path)


def identity(path):
    s = path.stat()
    return dict(device=s.st_dev, inode=s.st_ino, size_bytes=s.st_size,
                mtime_ns=s.st_mtime_ns, ctime_ns=s.st_ctime_ns)


def sha256(path, progress=False):
    before = identity(path)
    digest = hashlib.sha256()
    count = 0
    start = last = time.monotonic()
    with path.open('rb') as stream:
        while data := stream.read(8 * 1024 * 1024):
            digest.update(data)
            count += len(data)
            current = time.monotonic()
            if progress and current - last >= 5:
                print(f'[digest] {path} {100*count/max(before["size_bytes"], 1):.1f}% '
                      f'{count/1e9:.2f} GB elapsed={current-start:.1f}s', flush=True)
                last = current
    if before != identity(path):
        raise RuntimeError(f'input changed while hashing: {path}')
    return digest.hexdigest()


def inspect_input(path):
    entry = dict(path=str(path), identity=identity(path), status='eligible')
    try:
        with path.open('rb') as stream:
            header = stream.read(32)
        if len(header) != 32:
            raise ValueError('header shorter than 32 bytes')
        magic, version, buffer_size, written, dropped = struct.unpack('<IIQQQ', header)
        entry['header'] = dict(magic=magic, version=version, buffer_size=buffer_size,
                               written_records=written, dropped_records=dropped)
        if magic != 0x54524143 or version != 1:
            raise ValueError(f'invalid CART magic/version: {magic:#x}/{version}')
        if written > ((1 << 64) - 1 - 32) // 16:
            raise ValueError('record count overflows file size')
        expected = 32 + 16 * written
        if expected != entry['identity']['size_bytes']:
            raise ValueError(f'header implies {expected} bytes; actual {entry["identity"]["size_bytes"]}')
    except (OSError, ValueError) as exc:
        entry.update(status='invalid_input', error=str(exc))
    return entry


def verify_input(entry, prepared_host):
    """Check shared-file metadata without comparing host-local mount numbers.

    Device/inode pairs remain meaningful for comparisons *on the same node*.
    Across nodes, compare size, timestamps and the CART header. The analyzer
    checks its own local identity before/after reading and computes full SHA256.
    """
    path = Path(entry['path'])
    current = inspect_input(path)
    if current['status'] != 'eligible':
        raise ValueError(f'input validation failed: {current["error"]}')
    fields = ['size_bytes', 'mtime_ns', 'ctime_ns']
    if prepared_host == socket.gethostname():
        fields += ['device', 'inode']
    changes = {k: dict(prepared=entry['identity'][k], current=current['identity'][k])
               for k in fields if entry['identity'][k] != current['identity'][k]}
    if entry['header'] != current['header']:
        changes['header'] = dict(prepared=entry['header'], current=current['header'])
    if changes:
        raise ValueError('input changed since preparation: ' + json.dumps(changes, sort_keys=True))
    if identity(path) != current['identity']:
        raise ValueError('input changed during validation on execution node')
    return current['identity']


def inventory(roots):
    entries = []
    seen_paths = set()
    for root_index, root in enumerate(roots):
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f'not a directory: {root}')
        for path in sorted(root.rglob('*.bin')):
            canonical = path.resolve(strict=True)
            if str(path.absolute()) in seen_paths:
                continue
            seen_paths.add(str(path.absolute()))
            entry = inspect_input(canonical)
            relative = str(path.relative_to(root))
            label = re.sub(r'[^a-zA-Z0-9_-]+', '_', relative.removesuffix('.bin'))[:100]
            path_id = hashlib.sha256(str(path.absolute()).encode()).hexdigest()[:10]
            entry.update(trace_id=f'r{root_index}_{label}_{path_id}',
                         discovered_path=str(path.absolute()), dataset_root=str(root),
                         dataset_relative_path=relative)
            entries.append(entry)
    if not entries:
        raise ValueError('no .bin inputs found')
    # Only equal-size, byte-identical headers can be duplicates. First catch
    # hardlinks/symlinks; full SHA-256 distinguishes independent candidate copies.
    inodes, candidates = {}, {}
    for e in entries:
        if e['status'] != 'eligible':
            continue
        i = e['identity']
        key = (i['device'], i['inode'])
        if key in inodes:
            e.update(status='duplicate_alias', alias_of=inodes[key]['trace_id'], duplicate_proof='same_inode')
            continue
        inodes[key] = e
        h = e['header']
        key = (i['size_bytes'], h['buffer_size'], h['written_records'], h['dropped_records'])
        candidates.setdefault(key, []).append(e)
    for group in candidates.values():
        if len(group) < 2:
            continue
        print(f'Checking {len(group)} candidate copies with full SHA-256', flush=True)
        with ThreadPoolExecutor(max_workers=min(2, len(group))) as pool:
            digests = list(pool.map(lambda e: sha256(Path(e['path']), progress=True), group))
        seen = {}
        for e, digest in zip(group, digests):
            e['sha256'] = digest
            if digest in seen:
                e.update(status='duplicate_alias', alias_of=seen[digest]['trace_id'], duplicate_proof='full_sha256')
            else:
                seen[digest] = e
    # Collapse alias chains (an inode owner may itself have become a SHA alias).
    by_id = {e['trace_id']: e for e in entries}
    for e in entries:
        if e['status'] == 'duplicate_alias':
            while by_id[e['alias_of']]['status'] == 'duplicate_alias':
                e['alias_of'] = by_id[e['alias_of']]['alias_of']
    return entries


def git_output(*args):
    result = subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def snapshot_run(run, binary):
    binary = binary.resolve(strict=True)
    if not os.access(binary, os.X_OK):
        raise ValueError(f'not executable: {binary}')
    snapshot = run / 'snapshot'
    shutil.copy2(binary, snapshot / 'write_read_analyzer')
    for name in ('src', 'scripts', 'tests'):
        shutil.copytree(ROOT / name, snapshot / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('README.md', 'CMakeLists.txt', '.gitignore'):
        shutil.copy2(ROOT / name, snapshot / name)
    return dict(git_head=git_output('rev-parse', 'HEAD'), git_status=git_output('status', '--short'),
                binary_sha256=sha256(snapshot / 'write_read_analyzer'),
                source_sha256={str(p.relative_to(snapshot)): sha256(p)
                               for p in sorted(snapshot.rglob('*'))
                               if p.is_file() and p.name != 'write_read_analyzer'},
                host=socket.gethostname(), source_snapshot='snapshot/')


def publish_prepared(run, manifest):
    atomic_json(run / 'manifest.json', manifest)
    atomic_json(run / 'PREPARED.json', dict(prepared_utc=now(), task_count=manifest['task_count']))
    print(f'Prepared {run}\nEligible distinct traces to execute: {manifest["task_count"]}', flush=True)
    for e in manifest['inputs']:
        print(f'  {e["status"]:16} {e["trace_id"]} {e.get("error", e.get("alias_of", ""))}')


def prepare(args):
    run = args.run_dir.resolve()
    # Reserve the run before preparation so a failed prepare cannot be mistaken
    # for a complete snapshot or be silently overwritten by another invocation.
    run.mkdir(parents=True, exist_ok=False)
    for name in ('logs', 'results', 'status', 'snapshot'):
        (run / name).mkdir()
    try:
        entries = inventory(args.roots or DEFAULT_ROOTS)
        eligible = [e for e in entries if e['status'] == 'eligible']
        if not eligible:
            raise ValueError('no structurally valid distinct inputs')
        for index, e in enumerate(eligible):
            e['task_index'] = index
        manifest = dict(schema_version=1, created_utc=now(), run_dir=str(run),
                        roots=[str(p.resolve()) for p in (args.roots or DEFAULT_ROOTS)],
                        inputs=entries, task_count=len(eligible),
                        coverage='partial' if any(e['status'] == 'invalid_input' for e in entries) else 'pending',
                        options=dict(workers=args.workers, decode_workers=args.decode_workers,
                                     block_records=args.block_records, prefetch=args.prefetch,
                                     queue_batches=args.queue_batches, progress_seconds=args.progress_seconds),
                        resources=dict(cpus_per_task=args.cpus_per_task, memory=args.memory,
                                       max_parallel=args.max_parallel or len(eligible)),
                        input_validation_policy='node_aware_metadata_header_and_local_stability',
                        provenance=snapshot_run(run, args.binary))
        if args.cpus_per_task < args.workers + args.decode_workers + 2:
            raise ValueError('cpus-per-task must cover workers + decoders + dispatcher/progress')
        publish_prepared(run, manifest)
    except Exception as exc:
        atomic_json(run / 'PREPARE_FAILED.json', dict(error=str(exc), timestamp=now()))
        raise


def complete_result(path, entry, expected_sha256):
    required = ['COMPLETE', 'summary.json', 'summary.csv', 'time_histogram.csv',
                'intervening_operations_histogram.csv']
    if not all((path / name).is_file() for name in required):
        raise ValueError(f'cannot reuse incomplete result: {path}')
    summary = json.loads((path / 'summary.json').read_text())
    if not summary['full_trace'] or summary['raw_record_slots'] != entry['header']['written_records']:
        raise ValueError(f'cannot reuse partial/incorrect input result: {path}')
    if (summary['input']['path'] != entry['path'] or not expected_sha256
            or summary['input']['sha256'] != expected_sha256
            or entry.get('sha256', expected_sha256) != expected_sha256):
        raise ValueError(f'cannot reuse result with mismatched provenance: {path}')
    for group in summary['groups'].values():
        if (group['writes'] != group['matched_writes'] + group['superseded_writes'] + group['pending_writes_at_end']
                or group['reads'] != group['matched_writes'] + group['reads_without_pending_write']
                or group['valid_accesses'] != group['reads'] + group['writes']):
            raise ValueError(f'cannot reuse result with invalid counters: {path}')
        for metric in ('time_cycles', 'intervening_operations'):
            if sum(group[metric]['histogram_counts']) != group['matched_writes']:
                raise ValueError(f'cannot reuse result with invalid histogram: {path}')
    if (summary['groups']['combined']['valid_accesses'] + summary['invalid_record_slots']
            != summary['raw_record_slots']):
        raise ValueError(f'cannot reuse result with invalid record counts: {path}')


def retry(args):
    """Prepare failed tasks only; reference successful results without modifying them."""
    source = args.from_run.resolve(strict=True)
    parent = json.loads((source / 'manifest.json').read_text())
    run = args.run_dir.resolve()
    run.mkdir(parents=True, exist_ok=False)
    for name in ('logs', 'results', 'status', 'snapshot'):
        (run / name).mkdir()
    try:
        entries, task_count, reused = parent['inputs'], 0, 0
        for e in entries:
            if e['status'] not in ('eligible', 'reused_result'):
                continue
            local_identity = verify_input(e, parent['provenance']['host'])
            if e['status'] == 'reused_result':
                result = Path(e['result'])
                digest = e['sha256']
            else:
                status_path = source / 'status' / (e['trace_id'] + '.json')
                status = json.loads(status_path.read_text()) if status_path.exists() else {}
                old_index = e.pop('task_index')
                e['parent_task_index'] = old_index
                if status.get('status') == 'failed_task':
                    e.update(task_index=task_count, identity=local_identity)
                    task_count += 1
                    continue
                if status.get('status') != 'analyzed':
                    raise ValueError(f'refusing retry of nonterminal/missing task: {e["trace_id"]}')
                result = source / 'results' / e['trace_id']
                digest = status.get('sha256')
            complete_result(result, e, digest)
            e.update(status='reused_result', result=str(result), sha256=digest,
                     identity=local_identity, reused_from_run=str(source))
            reused += 1
        if not task_count:
            raise ValueError('no failed tasks to retry')
        provenance = snapshot_run(run, args.binary)
        if provenance['binary_sha256'] != parent['provenance']['binary_sha256']:
            raise ValueError('retry requires the same analyzer binary; use prepare for a changed analyzer')
        resources = dict(parent['resources'], max_parallel=args.max_parallel or task_count)
        if args.memory:
            resources['memory'] = args.memory
        manifest = dict(schema_version=1, created_utc=now(), run_dir=str(run), roots=parent['roots'],
                        inputs=entries, task_count=task_count, reused_result_count=reused,
                        retry_of=str(source), options=parent['options'], resources=resources,
                        coverage='partial' if any(e['status'] == 'invalid_input' for e in entries) else 'pending',
                        input_validation_policy='node_aware_metadata_header_and_local_stability',
                        provenance=provenance)
        publish_prepared(run, manifest)
        print(f'Reusing {reused} complete results from {source}; old run remains untouched.', flush=True)
    except Exception as exc:
        atomic_json(run / 'PREPARE_FAILED.json', dict(error=str(exc), timestamp=now()))
        raise


def submit(args):
    run = args.run_dir.resolve(strict=True)
    if not (run / 'PREPARED.json').is_file():
        raise ValueError('run was not completely prepared')
    # Exclusive submission claim prevents accidental duplicate jobs. A rejected
    # submission stays visibly failed and is not automatically retried.
    with (run / 'SUBMIT_CLAIM.json').open('x') as stream:
        json.dump(dict(timestamp=now()), stream)
    manifest = json.loads((run / 'manifest.json').read_text())
    r = manifest['resources']
    cmd = ['sbatch', '--parsable', '--job-name=write-read', '--nodes=1', '--ntasks=1',
           f'--cpus-per-task={r["cpus_per_task"]}', f'--mem={r["memory"]}',
           f'--array=0-{manifest["task_count"]-1}%{r["max_parallel"]}',
           f'--chdir={run}', f'--output={run}/logs/slurm-%A_%a.out',
           f'--error={run}/logs/slurm-%A_%a.err',
           str(run / 'snapshot/scripts/analyze_array.sbatch'), str(run)]
    if args.partition:
        cmd.insert(1, f'--partition={args.partition}')
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode:
        atomic_json(run / 'SUBMIT_FAILED.json', dict(command=cmd, stderr=result.stderr, timestamp=now()))
        raise RuntimeError(result.stderr.strip())
    job_id = result.stdout.strip().split(';')[0]
    if not job_id.isdigit():
        raise RuntimeError(f'unrecognized sbatch reply: {result.stdout!r}; inspect queue before retry')
    atomic_json(run / 'submission.json', dict(job_id=job_id, command=cmd, submitted_utc=now()))
    print(f'Submitted job array {job_id}\nRun: {run}\nLogs: {run}/logs/\n'
          f'Tail one task: tail -f {run}/logs/slurm-{job_id}_0.out', flush=True)


def task(args):
    run = args.run_dir.resolve(strict=True)
    manifest = json.loads((run / 'manifest.json').read_text())
    entries = [e for e in manifest['inputs'] if e.get('task_index') == args.index]
    if len(entries) != 1:
        raise ValueError('task index missing or ambiguous')
    entry = entries[0]
    status = run / 'status' / f'{entry["trace_id"]}.json'
    # Claim outside the exception handler: a duplicate invocation must not
    # clobber the status file belonging to the first invocation.
    with (run / 'status' / f'{entry["trace_id"]}.claim').open('x') as stream:
        stream.write(now() + '\n')
    metadata = dict(trace_id=entry['trace_id'], task_index=args.index, input=entry['path'],
                    started_utc=now(), host=socket.gethostname(), slurm_job_id=os.getenv('SLURM_JOB_ID'),
                    slurm_array_job_id=os.getenv('SLURM_ARRAY_JOB_ID'))
    output = run / 'results' / entry['trace_id']
    try:
        atomic_json(status, dict(metadata, status='running'))
        local_identity = verify_input(entry, manifest['provenance']['host'])
        metadata['execution_input_identity'] = local_identity
        atomic_json(status, dict(metadata, status='running'))
        binary = run / 'snapshot/write_read_analyzer'
        if sha256(binary) != manifest['provenance']['binary_sha256']:
            raise ValueError('snapshot executable hash mismatch')
        cmd = [str(binary), '--input', entry['path'], '--output', str(output)]
        for k, v in manifest['options'].items():
            cmd += ['--' + k.replace('_', '-'), str(v)]
        print(f'Started {entry["trace_id"]} at {now()} on {socket.gethostname()}', flush=True)
        print(f'Input validated: node-local identity {json.dumps(local_identity, sort_keys=True)}', flush=True)
        print('Command: ' + ' '.join(cmd), flush=True)
        # The enclosing srun owns the 24-CPU task; do not nest another srun.
        result = subprocess.run(cmd, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f'analyzer exited {result.returncode}')
        summary = json.loads((output / 'summary.json').read_text())
        if not (output / 'COMPLETE').exists() or not summary['full_trace']:
            raise RuntimeError('analyzer did not publish a full-trace completion marker')
        if entry.get('sha256') and entry['sha256'] != summary['input']['sha256']:
            raise RuntimeError('full-file digest differs from preparation')
        if identity(Path(entry['path'])) != local_identity:
            raise RuntimeError('input changed on execution node during task')
        atomic_json(status, dict(metadata, status='analyzed', finished_utc=now(),
                                 result=str(output), sha256=summary['input']['sha256']))
        print(f'Finished {entry["trace_id"]} at {now()}', flush=True)
    except Exception as exc:
        atomic_json(status, dict(metadata, status='failed_task', finished_utc=now(), error=str(exc)))
        raise


def step_failed(args):
    """Best-effort batch-shell fallback if srun/the task process is killed."""
    run = args.run_dir.resolve(strict=True)
    manifest = json.loads((run / 'manifest.json').read_text())
    entry = next(e for e in manifest['inputs'] if e.get('task_index') == args.index)
    path = run / 'status' / f'{entry["trace_id"]}.json'
    previous = json.loads(path.read_text()) if path.exists() else {}
    if previous.get('status') in ('analyzed', 'failed_task'):
        return
    atomic_json(path, dict(previous, trace_id=entry['trace_id'], task_index=args.index,
                           status='failed_task', finished_utc=now(),
                           error=f'srun/batch step exited {args.exit_code}'))


def positive(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return n


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--run-dir', type=Path, required=True)
    prep.add_argument('--roots', nargs='+', type=Path)
    prep.add_argument('--binary', type=Path, default=ROOT / 'build/write_read_analyzer')
    prep.add_argument('--workers', type=positive, default=16)
    prep.add_argument('--decode-workers', type=positive, default=4)
    prep.add_argument('--block-records', type=positive, default=262144)
    prep.add_argument('--prefetch', type=positive, default=8)
    prep.add_argument('--queue-batches', type=positive, default=4)
    prep.add_argument('--progress-seconds', type=positive, default=5)
    prep.add_argument('--cpus-per-task', type=positive, default=24)
    prep.add_argument('--memory', default='64G')
    prep.add_argument('--max-parallel', type=positive)
    prep.set_defaults(func=prepare)
    again = sub.add_parser('retry', help='prepare only failed tasks, reusing complete results')
    again.add_argument('--from-run', type=Path, required=True)
    again.add_argument('--run-dir', type=Path, required=True)
    again.add_argument('--binary', type=Path, default=ROOT / 'build/write_read_analyzer')
    again.add_argument('--memory')
    again.add_argument('--max-parallel', type=positive)
    again.set_defaults(func=retry)
    launch = sub.add_parser('submit')
    launch.add_argument('--run-dir', type=Path, required=True)
    launch.add_argument('--partition')
    launch.set_defaults(func=submit)
    one = sub.add_parser('task')
    one.add_argument('--run-dir', type=Path, required=True)
    one.add_argument('--index', type=int, required=True)
    one.set_defaults(func=task)
    failed = sub.add_parser('step-failed')
    failed.add_argument('--run-dir', type=Path, required=True)
    failed.add_argument('--index', type=int, required=True)
    failed.add_argument('--exit-code', type=int, required=True)
    failed.set_defaults(func=step_failed)
    return p


if __name__ == '__main__':
    try:
        args = parser().parse_args()
        args.func(args)
    except Exception as exc:
        print(f'error: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
