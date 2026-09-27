# Write-to-read trace analysis

## Status

Design finalized and implementation added September 27, 2026. The C++20 analyzer,
parallel decoding/analysis, reference tests, and Slurm array workflow are ready.
The existing Git history and remote are preserved. **All ten eligible distinct
traces are analyzed, and the plots and reports are published.** Three malformed
inputs remain excluded; one duplicate BC copy is counted only once. Publication
therefore has complete eligible-trace coverage but partial discovered-input
coverage. No jobs are needed to regenerate the plots from the saved summaries.

The fresh **0.1-second execution-timeline run is also complete**: Slurm array
`186369` successfully re-analyzed all ten eligible distinct traces. Its timeline
counts conserve all original statistics, and every raw-file digest and matching/
distance result agrees with the earlier publication. The low-effort monitor
confirmed completion; plotting then ran locally.

## Results at a glance

[Plots, per-trace tables, and coverage](docs/results/write_read_20260927/README.md) ·
[Analysis and interpretation](docs/write_read_findings_20260927.md) ·
[All trace/channel statistics CSV](docs/results/write_read_20260927/summary.csv) ·
[Execution timelines (0.1 s)](docs/results/write_read_timeline_20260927/README.md)

The [timeline overview](docs/results/write_read_timeline_20260927/execution_timeline_overview.png)
shows when matched reads occur during each trace, not their write-to-read gaps.
Each trace also has a combined/channel-0/channel-1 PNG and exact per-window
CSV counts. There are **11 new PNGs**; previous plots remain unchanged.

Across **22,337,500,928 valid accesses**, the analyzer observed **2,336,407,811
writes** and **1,590,242,325 matched write→first-read pairs**:

| Observed write outcome | Count | Fraction of writes |
|---|---:|---:|
| Matched first subsequent read | 1,590,242,325 | 68.06% |
| Superseded by another write before a read | 101,585,762 | 4.35% |
| Still pending at the trace boundary | 644,579,724 | 27.59% |

![Per-trace write outcomes](docs/results/write_read_20260927/write_outcomes.png)

For matched pairs, pooled mean time distance is **442.42 million 400-MHz cycles
(1.106 seconds)**, and mean intervening traffic is **139.07 million accesses**.
Only **6.40%** of matched pairs are less than `2^20` cycles (2.62144 ms) apart.
These are elapsed **write-to-read gaps in the recorded stream**, not memory-service
latencies or cache hit rates. Pending writes can be read after the trace ends;
collector drops, especially in PR/XS, limit interpretation. See the analysis
note for the per-trace differences and exact percentile intervals.

### Regenerate plots locally

Install Matplotlib if needed (`python3 -m pip install -r requirements-plot.txt`),
then choose a **new** output directory:

```sh
python3 scripts/plot_results.py \
  --run-dir out/write_read_all_20260927_retry1 \
  --output docs/results/write_read_new
```

This reads JSON/CSV summaries only, follows successful-result references from
the retry manifest, validates counters/digests/histograms, and creates 23 PNGs:
two three-panel histograms per trace, two pooled histograms, and one write-outcome
overview. No Slurm allocation or raw-bin reparse is used. Every result includes
portable JSON/CSV snapshots and a README; the publisher also saves its own source
snapshot and provenance. Existing output directories are never overwritten.
Failed/incomplete results are rejected unless `--allow-partial` is explicitly
requested; invalid raw inputs always remain visible in the coverage report.

## Build, test, and launch

Dependencies: a C++20 compiler, CMake, OpenSSL development files (SHA-256), Python
3.9 or newer, and Slurm for cluster submission. Python orchestration uses only
the standard library; plotting additionally uses Matplotlib. Analysis itself is C++.

```sh
cd /research/yans3/gitdoc/tracing_analysis
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j 4
ctest --test-dir build --output-on-failure

# Prepare a fresh run: recursively inventory both default input roots,
# validate headers, confirm candidate duplicate copies by SHA-256, and snapshot
# the executable, sources, scripts, tests, options, and input identities.
run_dir="$PWD/out/write_read_$(date -u +%Y%m%d_%H%M%S)"
python3 scripts/workflow.py prepare --run-dir "$run_dir"
python3 scripts/workflow.py submit --run-dir "$run_dir"
```

Preparation prints any invalid inputs and duplicate aliases. It does not launch
the full analysis. Submission prints the array job ID and log paths and returns
immediately; it never waits for completion or starts a monitor. `submission.json`
records the ID. An existing run directory or submission claim cannot be reused
accidentally. Use a new run for retries, adjusting `prepare --memory`,
`--cpus-per-task`, `--workers`, `--decode-workers`, or `--max-parallel` as needed.
Alternative roots are accepted through `prepare --roots DIR [DIR ...]`.

To retry only failed tasks, preserving successful results and the old run:

```sh
python3 scripts/workflow.py retry \
  --from-run out/write_read_all_20260927_v1 \
  --run-dir out/write_read_all_20260927_retry1
python3 scripts/workflow.py submit --run-dir out/write_read_all_20260927_retry1
```

`retry` creates a new snapshot and manifest, retains invalid-input/duplicate
entries, and schedules only terminal `failed_task` entries. Successful full
results are verified and referenced as `reused_result` entries with an absolute
`result` path; they are not rerun, copied, or overwritten. A consumer of a retry
run must combine those references with that run's successful new task outputs,
once per distinct trace. Active/missing task statuses, incomplete successful
outputs, and a changed analyzer executable are rejected. The retry inherits
analysis settings; `--memory` and `--max-parallel` can change resource allocation.

Each distinct eligible trace gets one array task and one `srun` step. Defaults
are 24 CPUs, 64 GiB RAM, 16 analysis workers, and four decoder workers per task.
All array tasks are allowed to run concurrently; Slurm decides placement and
resource availability. No single-node restriction is imposed on the array,
although each individual trace task uses one node.

The printed command for one task's progress is:

```sh
tail -f "$run_dir/logs/slurm-ARRAY_JOB_ID_0.out"
```

Use the actual array ID and an index from `manifest.json`. Analyzer progress
appears every five seconds in `.out`; Slurm launch errors appear in `.err`.
Progress reports decoded raw slots, ordered-dispatched raw slots/percentage,
dispatched and processed valid events, raw throughput, elapsed time, estimated
remaining dispatch time, pending writes, queue high-water marks, and RSS peak.
At 100% dispatch, workers may still be draining: only successful task status and
the `COMPLETE` marker establish a completed full-trace result.

Run layout:

```text
out/write_read_TIMESTAMP/
  manifest.json                  discovered paths, exclusions, tasks, provenance
  submission.json                Slurm array ID and submission command
  snapshot/                      frozen executable and source/config snapshot
  logs/slurm-ARRAY_INDEX.{out,err}
  status/TRACE_ID.json            running / analyzed / failed_task
  results/TRACE_ID/
    summary.json                 full metadata, counters, exact sums, percentile intervals
    summary.csv                  compact combined / channel_0 / channel_1 summary
    time_histogram.csv
    intervening_operations_histogram.csv
    COMPLETE                     emitted last, only for successful full analysis
```

Exact distance sums in JSON are decimal strings to avoid downstream integer
precision loss. Percentile bounds are histogram intervals, not exact percentile
values. Undefined numeric CSV cells are blank; JSON values are `null`.
Histogram CSV fractions use matched pairs in their own group as the denominator.

### Execution timelines (0.1-second windows)

New `workflow.py prepare` runs enable `--timeline-bin-cycles 40000000` by
default: 0.1 seconds at the trace's 400-MHz clock. The standalone analyzer can
enable the same option; its default of zero retains histogram-only behavior.
This requires a **fresh analysis of the raw traces**, not failed-task retry or
rebinning the older saved histograms. No new capture, RTRACE conversion, or
expanded text trace is needed.

For every matched pair, increment the window containing the **read request**:
`(read_timestamp - first_valid_timestamp) / timeline_bin_cycles`, using integer
division. All workers and both channels use that same per-trace origin, even
when decoder blocks complete out of order. The pending-write map survives window
boundaries; the latest-write/first-read matching policy is unchanged.

`execution_timeline.csv` stores combined/channel-0/channel-1 counts of matched
pairs, reads, and writes, with exact half-open cycle offsets. `summary.json`
records the origin, last valid timestamp, width, and attribution. The CSV stores
only bins with observed events; absent bins are explicitly zero, so long idle
gaps do not allocate large dense arrays. Plots restore zero intervals and mark
the unobserved tail of the last window. Empty traces have null timestamp bounds.

Each timeline must sum to the original read/write/matched counters, and channels
must sum to the combined result in every bin. Batch completion/retry validation
requires the timeline artifact when the manifest requests it. Original outputs
and plots remain intact. Parsing still uses parallel decoder/analysis workers
and one Slurm array task per distinct trace; plotting runs locally from CSVs.

Counts measure **observed matched-read requests per window**, not response
completions or service latency. They depend on traffic intensity; per-window
read/write totals provide context. Collector drops cannot be localized to
windows from the header alone. Different workloads are not phase-aligned by
making their first recorded accesses time zero, so timelines are not pooled.

After the new run finishes, publish the timelines **locally**, without Slurm:

```sh
python3 scripts/plot_execution_timeline.py \
  --run-dir out/write_read_timeline_20260927_v1 \
  --baseline-publication docs/results/write_read_20260927 \
  --output docs/results/write_read_timeline_20260927
```

The optional baseline comparison requires identical input SHA-256 digests and
all original matching/distance statistics. Publication includes one three-panel
PNG per trace, an overview, portable CSV/JSON snapshots, and provenance. It
refuses incomplete tasks, missing/corrupt timelines, and existing output paths.

`manifest.json` is an immutable launch inventory, not a live results database;
per-task status files and summaries supply final status and full-file digests.
Each successful analysis verifies source identity again at completion. A task
failure preserves diagnostics and never qualifies for pooling. The batch shell
also records failed `srun` exits when possible; a whole-node failure or SIGKILL
can prevent that fallback, so a missing terminal status/marker must be treated
as incomplete by the publisher.

Cross-node validation compares file size, nanosecond modification/change times,
and all CART header fields. Device/inode identifiers are only compared when
preparation and execution occur on the same hostname: mount device numbers are
not portable between nodes. Every task additionally records its local identity
and checks it again after analysis, and the analyzer independently checks its
open file and path. Full-file SHA-256 is produced in the streaming read and is
compared with any digest already known from preparation. This is ordinary input
stability detection, not a guarantee against deliberate metadata-preserving
content tampering.

The first array (`186346`) completed original BFS/CC/PR but rejected seven other
tasks before parsing. The shared input device number is `2304` on spr5, `56` on
a0/a1, and `89` on spr2; all other recorded identity fields matched. Node-aware
validation fixes this false change detection without dropping local checks.

### Local smoke tests

```sh
build/write_read_analyzer \
  --input /research/yans3/gitdoc/remap_tracing/bc.bin \
  --output out/smoke_bc_NEW \
  --max-records 4000000 --progress-seconds 1
```

`--max-records` counts raw slots, including invalid slots. It always marks the
result as smoke-only, even if the limit covers the whole input, and produces
`SMOKE_COMPLETE`, never `COMPLETE`. Such results cannot enter a full-run pool.
No RTRACE files or expanded text copies are produced.

Tests compare the threaded analyzer with an independent Python sequential
reference, including 1/2/16 workers, block boundaries, forced decode reordering,
invalid slots, both channels, high address bits, same-cycle order, supersession,
U64 histogram boundaries, distance sums exceeding U64, empty groups, and failure
cancellation under queue backpressure. Workflow tests use a fake `sbatch` and do
not submit cluster jobs.

Validation on September 27: the original eight analyzer/four workflow tests
passed, and Valgrind reported zero memory errors and no lost
allocations on a threaded raw-input smoke test. The optional sanitizer build
could not link because this server lacks the GCC ASan runtime; it is not claimed
as a passing sanitizer run.
Additional workflow regressions cover cross-node device/inode differences,
same-node identity changes, timestamp/header changes, failed-only retry,
preservation of successful results, and rejection of incomplete/active inputs.
The execution-timeline extension passes all four CTest suites. Added regressions
cover exact window boundaries, common origins across workers/channels, matches
across windows, sparse U64-scale gaps, empty traces, timeline-required retries,
CSV conservation/corruption checks, baseline agreement, and PNG publication.

### Implementation and memory bounds

[`src/write_read_analyzer.cpp`](src/write_read_analyzer.cpp) reads raw CART in
262,144-slot blocks. Four decoder threads build per-owner event batches with
block-local **valid-event** indices. The dispatcher consumes decoded blocks in
file order, validates cross-block timestamps, updates a streaming SHA-256, and
attaches the global index base to each batch. Sixteen workers combine that base
with the local index and exclusively own their pending-line hash maps. A final
reduction produces both channels and their combined result.

At most eight decode blocks are prefetched; each worker input queue holds at
most four batches. These bounds limit transport memory independently of trace
length. Pending state is exact and unbounded by policy: it is never evicted.
The `sum_worker_pending_high_water` metric sums individual worker maxima and is
therefore a conservative upper bound, not necessarily a simultaneous peak.
Raw/event counts are bounded by the checked input file size; reductions use
checked additions. Distance sums use 128-bit integers, which cover the maximum
possible record count times the maximum 64-bit timestamp distance.

Four-million-slot smoke tests on BC, XS, and the newer CC trace passed, with
observed peak RSS at most about 596 MiB. These short prefixes **do not bound the
full-run pending state**. The existing full-page inventory of the older traces
has at most 4,015,168 distinct pages: at most 256,970,752 possible 64-byte lines.
Allowing roughly 64–128 bytes per retained pending line plus transport/allocator
overhead gives a conservative planning range of roughly 16–31 GiB for that
older inventory. The 64-GiB reservation provides headroom; the newer inputs do
not yet have an equivalent full-run bound. An out-of-memory task must fail and
be retried with more RAM, never silently discard observations.

## Confirmed analysis rules

Analyze memory-access traces at **64-byte granularity**, independently of any
cache or TLB model. The address key is the full recorded byte address shifted
right by six bits. Do not apply the cache simulator's address-width truncation.

Use **latest write to first subsequent read** semantics:

- A write starts a pending observation for its 64-byte line.
- A later write to the same line supersedes the pending write. Count the older
  write as superseded-before-read and replace its timestamp and event position.
- The first subsequent read of that line matches the pending write and clears
  it. Further reads form no new pair until another write occurs.
- Start with empty state independently for each input trace.
- Pending writes at end-of-trace are unmatched-at-end (right-censored).
- For equal timestamps, use original file record order. A read ordered after
  its matched write at the same timestamp has zero-cycle distance.

For example, `W(A,10), W(A,12), R(A,20), R(A,30)` yields one superseded write,
one matched write with distance eight cycles, and one read without a pending
write. This counts line-level access relationships. The trace does not establish
which bytes or values the read consumed, so supersession is defined at 64 bytes.

The primary match rate is `matched_writes / total_valid_writes`, including
superseded and unmatched-at-end writes in the denominator. Report zero-write
cases as undefined rather than dividing by zero. Histogram samples contain only
matched pairs; report unmatched categories alongside the histogram.

## Input representation and channels

The current CART parser in
[`remap_tracing_neo/src/cart.cpp`](../remap_tracing_neo/src/cart.cpp) and its
[`header definitions`](../remap_tracing_neo/include/remap_trace/cart.hpp) decode
16-byte little-endian records after a 32-byte file header:

- First 64-bit word: bit 63 is valid, bit 62 is write/read, and bits 51:0 hold
  the recorded byte address (`0x000fffffffffffff` mask).
- Second 64-bit word: timestamp, interpreted in the current workflow as
  400-MHz cycles.
- `channel = (address >> 6) & 1`.
- `source = (channel << 1) | is_write`.

This exposes **two address-interleaved channels and four direction-specific
source streams**:

| Source ID | Channel | Operation |
|---|---:|---|
| 0 | 0 | Read |
| 1 | 0 | Write |
| 2 | 1 | Read |
| 3 | 1 | Write |

These are the channels reconstructed by the trace reader. They do not establish
the number of downstream memory controllers, cores, software threads, or cache
banks. At 64-byte granularity, a given line has a fixed address bit 6, so its
write and read map to the same derived channel. Pair by line address across the
direction-specific streams: source 1 writes can match source 0 reads, and source
3 writes can match source 2 reads. Partitioning by the complete source ID would
incorrectly isolate writes from their reads.

**Existing RTRACE files are insufficient for this analysis.** Their payload
stores `address >> 12`, timestamp, and source. Source preserves address bit 6,
but bits 11:7 needed to identify the 64-byte line are lost. Read raw CART `.bin`
files; if a reusable compressed representation is introduced later, it must
retain the 64-byte line address, direction, timestamp, and original record order
and be distinguishable from the page-granularity RTRACE format.

Version 1 will stream raw CART records directly. The planned implementation is
a C++20 analyzer with Python/Matplotlib for local plotting. The existing parser
can serve as a format reference, but its page-address event representation must
not be reused for this analysis. A new compressed intermediate is outside the
initial implementation scope.

## Time histogram and intervening operations

The confirmed primary distance is `read_timestamp - write_timestamp` in
400-MHz cycles; one cycle equals 2.5 ns. Histogram bins are `[0]`, `[1]`,
`[2,3]`, `[4,7]`, `[8,15]`, and successive powers of two. Keep a distinct zero
bin and print exact inclusive boundaries in exported tables.

For each metric report count, exact minimum/maximum, mean, and the histogram
interval containing each of P50, P90, P95, and P99. Locate the percentile using
the nearest-rank definition over matched pairs. Label the returned lower/upper
bounds as a percentile interval; do not present a bucket midpoint as an exact
percentile. This keeps the summary bounded without retaining billions of pairs.

The **global intervening-operation histogram is confirmed** and uses its own
chart, separate from the time histogram. Use the same power-of-two bin scheme.
Assign each valid access its position in the entire original trace, across both
channels and including both reads and writes. For a matched pair:

```text
intervening_operations = read_event_index - write_event_index - 1
```

Both endpoints are excluded, so consecutive events have count zero. Invalid
records and unrecorded collector drops are excluded. Count individual accesses,
not unique addresses; idle cycles add time but add no operations.

| Event position | Cycle | Access |
|---|---:|---|
| 100 | 100 | Write line A |
| 101 | 101 | Read line B |
| 102 | 150 | Write line C |
| 103 | 180 | Read line A |

For A, elapsed time is 80 cycles (200 ns) and intervening-operation count is two.
With no B/C accesses, elapsed time would still be 80 cycles and operation count
would be zero. Thus time describes delay and operation count describes how much
observed traffic occurs during that delay. It is not a count of distinct lines
and does not imply a cache capacity requirement.

Counting only same-line intervening operations is uninformative under the
confirmed pairing rule: another write would supersede the selected write, and
an earlier read would already consume it. All reports, including per-channel
reports, use the same global event positions. A channel-0 pair can therefore
have intervening channel-1 accesses included in its operation distance.

## Confirmed chart layout

Produce **two separate PNGs per distinct trace**:

| File | Horizontal axis | Panels |
|---|---|---|
| `time_histogram.png` | Write-to-first-read distance in 400-MHz cycles | Combined, channel 0, channel 1 |
| `intervening_operations_histogram.png` | Number of intervening valid accesses across both channels | Combined, channel 0, channel 1 |

Each figure has three panels on one row. Use common bin boundaries and vertical
axis limits across the three panels in a figure. Present the power-of-two
intervals as ordered categorical bins, with an explicit zero bin; do not put
zero on a logarithmic numeric axis. Time and operation counts are never mixed
on the same axis.

The vertical axis is percentage of matched pairs **within the panel's group**.
Annotate each panel with its matched-pair count and write match rate so a small
matched subset is visible. CSVs retain exact counts, inclusive bin bounds, and
normalized fractions. Combined bin counts must equal the sum of the two channel
counts before normalization. Panel percentages need not add across channels.

For a group with no matched pairs, show "No matched pairs" and report distance
statistics/percentiles as undefined. Its match rate is zero if writes exist and
undefined if there are no writes. Empty input is a valid empty analysis if its
header is structurally valid.

Also produce the same two figures for the pooled result over distinct,
successfully analyzed full traces, with coverage and excluded-input statuses
listed. Compute this result by adding histogram counts, not averaging normalized
per-trace histograms. Keep every workload and THP variant available separately.

## Trace scope and initial inventory

The user requested **every available trace, including XS**. Discover raw inputs
under both established roots and keep each workload/THP variant separate:

- `/research/yans3/gitdoc/remap_tracing`
- `/research/yans3/trace`

Read-only discovery and CART header/file-size inspection on September 27 found
14 raw input paths. This is an initial inventory, not a full record validation:

| Location | Inputs | Header/size result |
|---|---|---|
| `gitdoc/remap_tracing` | `bc`, `bfs`, `cc`, `pr`, `xs_20t`, `xs_20t_300000p` | All six pass |
| `trace/trace_09_07` | BFS and CC, each with/without THP | All four pass |
| `trace/trace_09_07` | `bc_8g_noTHP`, `bc_8g_withTHP`, `pr_8g_withTHP` | All three have header/file-size mismatches |
| `trace` | `bc.bin` | Passes; potential duplicate of the original BC input |

The two `bc.bin` paths have different inodes but the same size and header counts.
The preparation pass confirmed their content equality using full-file SHA-256;
the duplicate is recorded as an alias and is not counted in the aggregate. For
future inputs, identify candidate duplicates with full-file SHA-256. Do not infer
equality from filename, size, or header alone. Canonicalize paths, identify inode
aliases first, and use unique trace IDs derived from dataset-relative paths so
the two `bc.bin` paths cannot overwrite one another's results.

Eleven paths currently pass the initial size checks. The three mismatched files
claim approximately 43 GB but contain approximately 15 GB. Keep them in the
manifest as validation failures requiring corrected input; do not silently
include a truncated prefix as a complete trace. Any intentional partial-file
analysis requires an explicit, separately labeled policy.

Nonzero collector-drop counts do not exclude a structurally valid trace.
Analyze all valid recorded accesses and report the drop metadata. Missing events
can hide writes, reads, and supersession; they can bias both match rates and
distances. Invalid record slots are counted separately from collector drops.

Record the input file identity, size, modification time, header fields, and
full-file digest in the manifest/provenance. Compute digests during the ordered
read where practical, and check file metadata again at completion to detect
ordinary concurrent input changes. Source files are expected to remain stable
for the duration of the run.

## Planned sbatch execution and parallelism

Use an `sbatch` job array with one task per distinct eligible trace and an
explicit manifest. Allow all tasks to run concurrently when CPU, memory, and
storage bandwidth permit. Slurm may place tasks on multiple machines. Raw
inputs remain read-only, and each task writes its own progress and result files.

Within each trace, use address ownership to parallelize the analysis itself:

1. Decode file blocks concurrently while retaining their original block order.
2. Restore record order and assign global valid-event positions before sharding.
   Advance the global position by each block's valid-event count, not its raw
   slot count. Check nondecreasing timestamps within and between nonempty blocks;
   invalid slots and empty blocks must not reset ordering checks.
3. Route each event by a deterministic hash of its full 64-byte line address.
4. Each analysis worker owns its lines and their pending writes exclusively.
   Preserve order within each worker's bounded input queue across all blocks.
5. Reduce independent counters and histograms when all workers drain.

All accesses to the same line reach the same worker, preserving matching and
supersession without shared-map locking. Original timestamps and event positions
travel with each event, so distances remain global despite sharding. Independent
time chunks cannot start with empty state because pairs can cross chunk boundaries.

A starting resource proposal is **24 CPUs per trace task**: 16 analysis workers,
four decode workers, and four CPUs of headroom for dispatch, I/O, and coordination.
With 11 currently eligible paths, that is up to 264 allocated CPUs; with a
confirmed BC duplicate removed, ten tasks would use 240 CPUs. This is a planned
allocation, not a guarantee that every core can stay busy: address skew, the
ordered dispatcher, and shared storage throughput may limit scaling.

An initial memory reservation of 64 GiB per task is a sizing proposal to validate
against pending-line state and queue bounds before full submission. Pending state
scales with distinct unread written lines. Use a small preparation/profile pass
plus conservative bounds to choose memory and concurrency; do not assume a short
prefix predicts the full-run maximum. Raising concurrency should not overcommit
RAM or make shared input I/O slower. Aggregate results and plot locally after all
successful jobs finish; report failed input/task statuses explicitly.

Bound decode prefetch, dispatcher batches, and worker queues independently of
file length. Pending writes cannot be evicted to meet a memory target: doing so
would change match counts and distances. Release pending entries on matched
reads; account for hash-table capacity and rehash peaks in memory estimates. A
resource failure produces a failed task and can be retried with more memory,
without publishing a partial trace as complete.

Progress should report decoded/processed events, percentage, throughput, elapsed
time, estimated remaining time, pending writes, and queue/memory high-water marks.
Raw write/read pair lists are not saved by default.

## Results and checks

For each distinct trace, export CSV/JSON and a Markdown report containing reads,
writes, matched writes, superseded writes, pending writes at end, reads without
a pending write, match rate, zero-distance matches, and matched-pair histograms.
Include source metadata, validity/drop counts, analysis options, and provenance.
Combined results plus channel 0/channel 1 breakdowns are confirmed and come from
the same pairing pass. Attribute each pair to its line's derived channel. Store
pending state as line key, latest-write timestamp, and global write-event index;
the channel can be derived from the line key.

Use a new run directory containing an input manifest, a configuration/provenance
snapshot, and separate per-trace logs and results. Each successful trace emits
summary JSON, summary CSV, a time histogram CSV, and an operation histogram CSV.
The local publication step adds the two PNGs and a README linking them. Publish
trace completion markers only after draining workers and validating counters;
write output files atomically and refuse to overwrite an existing run directory.

A final coverage report accounts for every discovered path as analyzed,
duplicate alias, invalid input, or failed job. Only successful full traces enter
the pooled result. If any inputs are invalid or jobs failed, label coverage as
partial and list them explicitly. Record-limited smoke results remain separate
and cannot enter the published full-run aggregate.

Required conservation identities:

```text
total_writes = matched_writes + superseded_writes + pending_writes_at_end
total_reads = matched_writes + reads_without_pending_write
total_valid_accesses = total_reads + total_writes
raw_record_slots = total_valid_accesses + invalid_record_slots
sum(time_histogram_counts) = matched_writes
sum(operation_histogram_counts) = matched_writes
```

Apply write/read identities per channel as well as globally, and require the
two channel counters and each histogram bin to sum to the combined values.
Use 64-bit counters/event indices with checked increments and sufficiently wide
accumulators (for example, 128-bit integers) for sums of distances. Never wrap a
distance sum silently when computing a mean over billions of accesses.

Aggregate match rates must weight by writes. Aggregate distance statistics must
weight by matched pairs. Neither is a simple mean of per-trace percentages.
Same-timestamp order is observation order in the trace, not proof of physical
completion or memory-ordering causality.

## Review and implementation acceptance

The review resolved the main correctness risks:

- Address fidelity: consume full raw addresses and distinguish lines within the
  same 4-KiB page, including lines with the same derived channel.
- Direction pairing: write/read source IDs remain connected by line address.
- Ordering: equal timestamps retain file order; pending writes survive block
  boundaries; global operation indices are assigned before sharding.
- Meaning of matches: counts describe observed 64-byte access relationships;
  neither per-byte data consumption nor unrecorded events can be inferred.
- Exactness under load: bounded transport queues apply backpressure; pending
  write state is never discarded for capacity reasons.
- Publication: incomplete inputs/runs and duplicate copies cannot silently
  affect the aggregate; empty groups and percentile intervals are explicit.

Before full execution, use small hand-computed sequences to check supersession,
first-read consumption, repeated reads, unread tail writes, two lines in one
page, both channels, invalid slots, equal timestamps in both W/R orders, empty
groups, and histogram boundaries. Include cross-block pairs and deliberately
out-of-order decode completion. A sequential reference and the parallel analyzer
must produce identical counters, exact distance sums, and histograms across
different worker counts and block sizes. Verify failures for malformed headers,
decreasing timestamps, incomplete outputs, and record-limited publication.

Implementation sequence:

1. Input inventory, format-preserving raw decoder, and sequential pairing
   reference with the confirmed metrics.
2. Ordered parallel decoding and address-sharded analysis, checked against the
   reference, followed by progress reporting and bounded-memory profiling.
3. Slurm array launcher with manifest, provenance, independent outputs, and
   explicit failure reporting; choose concurrency from measured throughput and
   conservative memory requirements.
4. Run every eligible distinct trace after validation; then publish separate
   time/operation figures, per-channel results, pooled results, and coverage.

No analysis-definition questions remain. CPU/memory tuning is an implementation
detail within this plan. The three malformed inputs remain excluded until
corrected input is supplied; the BC duplicate is confirmed and deduplicated.
Implementation, full-run aggregation, coverage reporting, and local publication
are complete for the ten eligible traces. Publication is an explicit local
command and is not launched automatically by the Slurm workflow.
