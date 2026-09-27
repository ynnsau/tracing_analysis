"""Validate sparse execution-timeline artifacts without reading raw traces."""
import csv

GROUPS = ('combined', 'channel_0', 'channel_1')
COUNTERS = ('reads', 'writes', 'matched_pairs')
U64_MAX = (1 << 64) - 1


def require(condition, message):
    if not condition:
        raise ValueError('timeline: ' + message)


def integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= U64_MAX


def validate_timeline(folder, summary, expected_bin_cycles=None):
    meta = summary.get('timeline')
    require(isinstance(meta, dict), 'missing timeline; re-analyze this trace')
    require(meta.get('schema_version') == 1 and summary['timestamp_mhz'] == 400, 'unsupported schema/clock')
    width = meta.get('bin_cycles')
    require(integer(width) and width > 0, 'invalid bin width')
    require(summary.get('options', {}).get('timeline_bin_cycles') == width, 'option/metadata width mismatch')
    if expected_bin_cycles is not None:
        require(width == expected_bin_cycles, 'manifest/metadata width mismatch')
    require(meta.get('attribution') == 'matched_read_request', 'wrong event attribution')
    require(meta.get('storage') == 'sparse_event_bins' and meta.get('empty_bins_are_zero') is True,
            'unsupported sparse semantics')
    require(meta.get('csv') == 'execution_timeline.csv', 'unexpected CSV name')
    first, last, last_bin = (meta.get(k) for k in ('origin_timestamp', 'last_timestamp', 'last_bin'))
    populated = summary['groups']['combined']['valid_accesses'] > 0
    if populated:
        require(integer(first) and integer(last) and first <= last, 'invalid trace span')
        require(integer(last_bin) and last_bin == (last-first)//width, 'wrong last bin')
    else:
        require(first is None and last is None and last_bin is None, 'nonempty span for empty trace')
    data = {g: {} for g in GROUPS}
    with (folder/'execution_timeline.csv').open() as stream:
        reader = csv.DictReader(stream)
        require(reader.fieldnames == ['group', 'bin', 'start_offset_cycles', 'end_offset_cycles_exclusive',
                                     *COUNTERS], 'wrong CSV columns')
        for row in reader:
            group = row['group']
            require(group in data, 'unknown group')
            values = {k: int(row[k]) for k in row if k != 'group'}
            b = values['bin']
            require(populated and 0 <= b <= last_bin, 'bin outside trace span')
            require(b not in data[group], 'duplicate bin')
            require(values['start_offset_cycles'] == b*width
                    and values['end_offset_cycles_exclusive'] == (b+1)*width, 'incorrect bin bounds')
            require(all(integer(values[k]) for k in COUNTERS), 'invalid counter')
            require(values['matched_pairs'] <= values['reads'], 'matches exceed reads')
            data[group][b] = {k: values[k] for k in COUNTERS}
    bins = set(data['combined'])
    require(all(set(data[g]) == bins for g in GROUPS), 'channel bin sets differ')
    if populated:
        require(0 in bins and last_bin in bins, 'missing first/last event bin')
    for b in bins:
        require(data['combined'][b]['reads'] + data['combined'][b]['writes'] > 0, 'empty occupied bin')
        require(all(data['combined'][b][k] == data['channel_0'][b][k] + data['channel_1'][b][k]
                    for k in COUNTERS), 'channel conservation failure')
    for group in GROUPS:
        for counter in COUNTERS:
            expected = summary['groups'][group]['matched_writes' if counter == 'matched_pairs' else counter]
            require(sum(row[counter] for row in data[group].values()) == expected,
                    f'{group}/{counter} total differs from summary')
    return data
