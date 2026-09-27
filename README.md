# Write-to-read trace analysis

## Status

Design finalized and reviewed September 27, 2026. This repository contains only
this README and Git metadata. The analysis definitions and chart layout below
are confirmed. Implementation and job submission remain future work. Git is
initialized on `main`, with no commit or remote yet.

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
Their content equality has not been checked. Identify duplicate contents during
the eventual preparation pass using full-file SHA-256 for candidate duplicates
so aliases are reported without double-counting the aggregate. Do not infer
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
detail within this plan. The three malformed inputs and potential BC duplicate
remain input-preparation issues with the handling specified above. This revision
finalizes the documentation only; implementation and submission await the next
instruction.
