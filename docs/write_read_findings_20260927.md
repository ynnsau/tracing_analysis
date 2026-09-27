# What the write-to-read traces show

Analysis of the September 27 publication: [all charts, per-trace statistics, and
coverage](results/write_read_20260927/README.md). The
[complete CSV](results/write_read_20260927/summary.csv) contains combined,
channel-0, and channel-1 statistics, exact distance sums, percentile intervals,
and short-distance fractions.

## 1. Many writes are read later, but usually not immediately

The ten eligible traces contain **22,337,500,928 valid accesses**, including
**2,336,407,811 writes**. Under the agreed **latest write → first subsequent read
of the same 64-byte line** rule:

| Outcome | Count | Fraction of all writes |
|---|---:|---:|
| Matched a subsequent read | 1,590,242,325 | 68.06% |
| Superseded by another write before a read | 101,585,762 | 4.35% |
| Still pending at the trace boundary | 644,579,724 | 27.59% |

![Write outcomes by trace](results/write_read_20260927/write_outcomes.png)

Most unmatched writes are therefore pending at the boundary, not superseded.
Neither category proves that the written bytes are useless: pending writes may
be read after capture ends, and CPU reads served by retained cache copies are
invisible at the memory tracing interface. These statistics describe observed
line-level access relationships, not byte-level data dependencies or cache hit
rates. In particular, a full-line dirty writeback must not be interpreted as an
individual CPU store that updates only some bytes of the line.

The first-read matches account for **7.95% of recorded reads**, a different
denominator from the 68.06% write match rate. Reads without a pending write can
still be repeated reads of data that was written and already matched earlier,
or reads of data written before the trace began. They are not necessarily
independent of writes.

### Interpreting write → write observations below the CPU caches

“Superseded before read” means another write to the same 64-byte line was
**observed at the tracing interface** before another read. It does not establish
that the CPU never read or used the earlier data, or that each observed write
was a capacity eviction that invalidated every CPU-cached copy.

Possible mechanisms for a legitimate `W(A) → W(A)` observation include:

- **Full-line overwrite without fetching the old contents.** On processors
  supporting ownership-only transactions such as ItoM, the CPU can acquire
  ownership for a complete overwrite and later evict the new dirty line without
  reading the previous contents. Whether this occurs depends on the processor
  and store sequence; a full-line overwrite does not always avoid a read.
- **Writeback without invalidation.** CLWB can write back a dirty line while
  retaining a clean cached copy. A subsequent store can dirty that resident line
  again, followed by another writeback. CPU reads hitting the retained copy would
  also be invisible to this trace.
- **Streaming/non-temporal writes.** These can write memory without first loading
  the destination. Such writes are not ordinary dirty-cache evictions.

These transaction distinctions are documented in the
[Intel uncore reference, opcode table](https://cdrdv2-public.intel.com/679093/639778%20ICX%20UPG%20v1.pdf).
They are **candidate explanations, not mechanisms established for these traces**.

By contrast, after a line is removed from all CPU caches, an ordinary partial
store generally needs a read-for-ownership (RFO) to retrieve the old contents.
There need not be a demand-load instruction, but if the RFO requires data from
the traced memory and is captured, the interface sequence is `W → R → W`, not
`W → W`. Intel explicitly identifies RFOs as store-initiated requests in its
[performance-event documentation](https://perfmon-events.intel.com/platforms/icelakex/core-events/core/).

The current records retain address, read/write direction, and timestamp, not the
originating CPU instruction or coherence opcode. Before attributing supersession
to a CPU mechanism, verify read coverage (including store-triggered reads),
collector drops, read/write ordering, and that each accepted transaction is
recorded exactly once. No collector audit or CPU-mechanism attribution was
performed for this publication. This qualification changes the interpretation,
not the matching rule or reported counts.

## 2. The matched distances are long and broadly distributed

| Matched-pair metric | Mean | P50 interval | P90 interval |
|---|---:|---|---|
| Time, 400-MHz cycles | 442,418,840.11 | [33,554,432, 67,108,863] | [1,073,741,824, 2,147,483,647] |
| Global intervening operations | 139,065,557.21 | [16,777,216, 33,554,431] | [268,435,456, 536,870,911] |

The mean time gap is **1.106 seconds**. The median falls in approximately
**83.89–167.77 ms**, and P90 falls in **2.684–5.369 seconds**. These are bin
intervals, not exact percentile estimates. The much larger mean than median
interval is consistent with the substantial long-distance tail.

![Pooled time distances](results/write_read_20260927/pooled/time_histogram.png)

![Pooled operation distances](results/write_read_20260927/pooled/intervening_operations_histogram.png)

Exact cumulative fractions at saved bin boundaries are:

| Distance threshold | Fraction of matched pairs below it |
|---|---:|
| Time < 1,024 cycles = 2.56 μs | 0.0098% |
| Time < 1,048,576 cycles = 2.62144 ms | 6.4045% |
| Intervening operations < 1,024 | 0.0658% |
| Intervening operations < 1,048,576 | 10.0675% |

Thus, a high eventual match rate does **not** imply frequent prompt
read-after-write use. For example, only **4.36% of all observed writes** both
match a read and do so within `2^20` cycles. That is an observed age-threshold
fraction, not a predicted buffer/cache hit rate: there is no capacity, eviction,
or service-latency model here.

These gaps are consistent with reuse across phases or iterations, but the
histograms cannot establish that explanation. They measure elapsed time between
recorded accesses, **not** the time taken to service a read. Likewise, intervening
operations count all observed traffic, not distinct lines; multiplying that
count by 64 bytes would not estimate a required cache capacity.

## 3. Workloads differ substantially

| Original trace | Write match rate | Pending fraction of writes | Mean matched time gap |
|---|---:|---:|---:|
| BC | 78.72% | 17.18% | 0.393 s |
| BFS | 66.74% | 27.19% | 1.130 s |
| CC | 52.98% | 38.78% | 1.792 s |
| PR | 75.88% | 19.51% | 1.295 s |
| XS 20t | 70.18% | 29.58% | 5.572 s |
| XS 20t / 300000p | 28.32% | 71.44% | 1.511 s |

BC has the highest observed match rate and a shorter mean gap than the other
original workloads. Original CC has more pending writes and the highest
supersession fraction among these traces, **8.235%**. XS 20t is an extreme
long-gap case despite its relatively high eventual match rate.

The low rate of the XS 300000p run is dominated by **71.44% pending at the
boundary**, not overwriting: only **0.239%** of its writes are superseded.
It records a similar number of writes to XS 20t (52.75 million versus 53.26
million), but far fewer total accesses (0.649 billion versus 4.698 billion).
This is consistent with an observation window that ends before many later reads,
but does not prove that cause: these are distinct runs, not verified prefixes of
one execution. The different configurations and traffic phases also matter.

Pooling is not an equal-weight average of workloads. BC supplies **40.33% of all
matched pairs**, whereas XS 20t supplies only **2.35%**, despite its very large
number of reads. The pooled plots therefore obscure some important workload
differences; use the individual plots when evaluating a particular workload.

## 4. The newer THP traces have fewer matches overall, but more short-gap matches

| New trace | Write match rate | Mean matched gap | Matched pairs with time < 2.62144 ms | Matched pairs with < 1,048,576 intervening operations |
|---|---:|---:|---:|---:|
| BFS, no THP | 64.89% | 1.855 s | 3.82% | 10.49% |
| BFS, THP | 58.92% | 1.236 s | 32.54% | 37.34% |
| CC, no THP | 60.01% | 1.600 s | 2.63% | 11.59% |
| CC, THP | 51.78% | 0.927 s | 33.22% | 38.76% |

The THP traces have a much larger short-distance component **conditional on a
match**, while their overall write match rates fall by **5.97 percentage points
for BFS** and **8.23 points for CC**. Their pending fractions rise to 41.07% and
45.75%, respectively. So “fewer eventual matches” and “shorter gaps among matched
pairs” coexist; one cannot substitute for the other.

The separate operation histograms show that the short-distance component is not
just a change in elapsed clock time: there are fewer intervening recorded
accesses for a substantial subset too. These are marginal distributions,
however; we cannot establish which specific pairs are short in **both** metrics.

All four newer traces report zero collector drops. Still, differences between
these separately recorded runs do not isolate a causal THP effect: total traffic,
write counts, capture boundaries, and execution phases differ as well.

## 5. Channels are closely balanced

Pooled write match rates are **68.0645% on channel 0** and **68.0626% on channel
1**. The largest per-trace difference is only **0.3214 percentage points**, in
the newer CC/THP trace. The corresponding histogram shapes are also visually
similar in the three-panel plots.

There is no obvious channel-specific write/read locality imbalance in these
summaries. This does not establish equal bandwidth, queue occupancy, or behavior
over short time windows. The channels here are the two address-bit-derived
partitions, not measurements of CPU threads or downstream controller counts.

## 6. Confidence, exclusions, and useful next analyses

All ten eligible distinct traces passed full-result, counter-conservation,
per-channel, exact-sum, percentile-interval, and JSON/CSV histogram validation.
There are **44 invalid record slots** in total, separate from collector drops.
One duplicate BC input is excluded from pooling. Three truncated newer BC/PR
inputs remain excluded, so coverage is explicitly **partial** across all
discovered input paths. The [coverage report](results/write_read_20260927/coverage.json)
lists every path and reason.

Original PR reports **583,273,912 dropped accesses**, and XS 20t reports
**742,705,516**. Using `dropped / (stored raw slots + dropped)` as the denominator,
these are approximately **11.04%** and **13.65%**, respectively. Missing events
can remove reads, writes, or superseding writes; the resulting bias is not
necessarily in one direction. The extremely long gaps are not solely a
drop-related observation: the zero-drop BFS and newer runs also have long
matched distances.

Useful follow-ups, **not performed in this publication**, would be:

- Break matching and distance distributions into execution windows to distinguish
  initialization, steady-state activity, and end-of-capture effects.
- Record the age of pending writes at the boundary to understand right-censoring.
- Collect a joint time-distance × operation-distance distribution if the goal
  is to distinguish idle intervals from intervening traffic on individual pairs.

Those extensions require another analysis pass or additional retained summaries;
the current separate marginal histograms cannot reconstruct them. Existing
plots can be regenerated locally without rereading the raw traces.
