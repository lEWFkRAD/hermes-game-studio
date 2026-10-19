"""Read-only, bounded-memory validation of a completed native TRELLIS post dump.

Requires only Python's standard library. This checks serialization and numeric
integrity, not topology, visual quality, or suitability for asset production.
Pinned writer: trellis.cpp 883346efb49bd3b46875ea996cb9f195b954d525,
src/trellis_cli.cpp:586-613. Coordinates are xyz integer cell indices: the
writer stores c[0],c[1],c[2]; ss_decoder.cpp:127-141 starts at [0,out_res),
and sparse.cpp:380 subdivides each component to 2*parent + {0,1}.
"""
import argparse
from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import struct
import sys
import time


CHUNK_ROWS = 65536  # Largest chunk: six float32 channels, 1.5 MiB.
MAX_RECORDS = 100_000_000
UPSTREAM = '883346efb49bd3b46875ea996cb9f195b954d525'


class InvalidDump(Exception):
    pass


def identity(value):
    # Windows path.stat and fstat expose different legacy ctime values on
    # Python 3.14. Compare ctime only between handle snapshots below.
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def scan(path, max_bytes, max_seconds):
    began = time.monotonic()
    result = {
        'schema': 'trellis-post-dump-inspection-v1',
        'input': str(path.absolute()),
        'upstream_commit': UPSTREAM,
        'byte_order': 'little',
        'coordinate_order': ['x', 'y', 'z'],
        'pbr_channels': ['base_r', 'base_g', 'base_b', 'metallic', 'roughness', 'alpha'],
        'limits': {'max_bytes': max_bytes, 'max_records_per_section': MAX_RECORDS,
                   'max_seconds': max_seconds, 'chunk_rows': CHUNK_ROWS},
        'sections': {}, 'errors': [], 'warnings': [], 'sha256': None,
        'bytes_hashed': 0,
    }
    hasher = hashlib.sha256()

    def read_exact(stream, size):
        if time.monotonic() - began > max_seconds:
            raise InvalidDump('Scan time limit exceeded')
        data = stream.read(size)
        hasher.update(data)
        result['bytes_hashed'] += len(data)
        if len(data) != size:
            raise InvalidDump('Input was truncated during scan')
        return data

    try:
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise InvalidDump('Input must be a regular file')
            result['actual_bytes'] = before.st_size
            if before.st_size < 16 or before.st_size > max_bytes:
                raise InvalidDump('File size is outside the configured bounds')
            V, F, Mv, res = struct.unpack('<4i', read_exact(stream, 16))
            result['header'] = {'vertices': V, 'faces': F, 'pbr_voxels': Mv, 'pbr_resolution': res}
            if not (0 < V <= MAX_RECORDS and 0 < F <= MAX_RECORDS and 0 <= Mv <= MAX_RECORDS):
                raise InvalidDump('Unsafe, empty geometry, or negative section counts')
            if res not in (512, 1024, 1536):
                raise InvalidDump('Unsupported PBR grid resolution; expected 512, 1024, or 1536')
            expected = 16 + 12 * V + 12 * F + 36 * Mv
            result['expected_bytes'] = expected
            if expected != before.st_size or expected > max_bytes:
                raise InvalidDump('Expected serialized length does not match file size')
            if Mv == 0:
                result['warnings'].append('Dump has no PBR voxels; geometry-only serialization is valid')

            for name, rows, columns, typecode in (
                    ('vertices', V, 3, 'f'), ('faces', F, 3, 'i'),
                    ('coords', Mv, 3, 'i'), ('pbr6', Mv, 6, 'f')):
                lower, upper = [None] * columns, [None] * columns
                nonfinite, out_of_bounds = 0, 0
                byte_offset = stream.tell()
                for start in range(0, rows, CHUNK_ROWS):
                    count = min(CHUNK_ROWS, rows - start)
                    values = array(typecode)
                    if values.itemsize != 4:
                        raise InvalidDump('Host array type does not have the required four-byte size')
                    values.frombytes(read_exact(stream, count * columns * 4))
                    if sys.byteorder != 'little':
                        values.byteswap()
                    for axis in range(columns):
                        component = values[axis::columns]
                        if typecode == 'f':
                            bad = sum(not math.isfinite(x) for x in component)
                            nonfinite += bad
                            if bad:
                                component = array('f', (x for x in component if math.isfinite(x)))
                        if not component:
                            continue
                        lo, hi = min(component), max(component)
                        lower[axis] = lo if lower[axis] is None else min(lower[axis], lo)
                        upper[axis] = hi if upper[axis] is None else max(upper[axis], hi)
                        if name == 'faces' and (lo < 0 or hi >= V):
                            out_of_bounds += sum(x < 0 or x >= V for x in component)
                        elif name == 'coords' and (lo < 0 or hi >= res):
                            out_of_bounds += sum(x < 0 or x >= res for x in component)
                entry = {'rows': rows, 'columns': columns, 'offset_bytes': byte_offset,
                         'minimum': lower, 'maximum': upper,
                         'nonfinite_values': nonfinite, 'out_of_bounds_values': out_of_bounds}
                result['sections'][name] = entry
                if nonfinite:
                    result['errors'].append(f'{name} contains {nonfinite} nonfinite values')
                if out_of_bounds:
                    result['errors'].append(f'{name} contains {out_of_bounds} out-of-bounds values')
                if name == 'pbr6' and rows and any(
                        (lo is not None and lo < 0.0) or (hi is not None and hi > 1.0)
                        for lo, hi in zip(lower, upper)):
                    result['warnings'].append('PBR values extend outside the documented [0,1] range')

            if stream.tell() != expected:
                raise InvalidDump('Section offsets do not end at expected file length')
            after = os.fstat(stream.fileno())
            current = path.stat()
            result['input_unchanged'] = (identity(before) == identity(after) == identity(current)
                                         and before.st_ctime_ns == after.st_ctime_ns)
            result['sha256'] = hasher.hexdigest()
            if not result['input_unchanged']:
                raise InvalidDump('Input changed or was replaced during scan')
    except (InvalidDump, OSError, struct.error, OverflowError) as error:
        result['errors'].append(str(error))
    result['elapsed_seconds'] = round(time.monotonic() - began, 6)
    result['status'] = 'failed' if result['errors'] else 'passed'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dump', type=Path)
    parser.add_argument('--report', required=True, type=Path, help='New JSON path; never overwrites an existing report')
    parser.add_argument('--max-bytes', type=int, default=8 * 1024 ** 3)
    parser.add_argument('--max-seconds', type=float, default=300.0)
    args = parser.parse_args()
    if not (16 <= args.max_bytes <= 16 * 1024 ** 3):
        parser.error('--max-bytes must be between 16 bytes and 16 GiB')
    if not (0 < args.max_seconds <= 1800) or not math.isfinite(args.max_seconds):
        parser.error('--max-seconds must be positive, finite, and at most 1800')
    # Reserve the output before scanning, so a competing/existing report always wins.
    # An interruption can leave an empty report reservation; choose a fresh path to retry.
    with args.report.open('x', encoding='utf-8', newline='\n') as output:
        report = scan(args.dump, args.max_bytes, args.max_seconds)
        json.dump(report, output, indent=2, allow_nan=False)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    print(json.dumps({key: report[key] for key in
                      ('status', 'input', 'sha256', 'bytes_hashed', 'elapsed_seconds', 'errors', 'warnings')}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
