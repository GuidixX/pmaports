#!/usr/bin/env python3
"""Concatenate FDTs while retaining an eight-byte-aligned base for each tree."""

from pathlib import Path
import struct
import sys


def aligned(dtb):
    if len(dtb) < 40 or dtb[:4] != b'\xd0\x0d\xfe\xed':
        raise ValueError('Invalid FDT header')
    total = struct.unpack_from('>I', dtb, 4)[0]
    if total != len(dtb):
        raise ValueError('FDT totalsize does not match its file size')
    result = bytearray(dtb)
    result.extend(b'\0' * (-len(result) % 8))
    # A concatenated-DTB reader advances by totalsize, so padding belongs
    # to the FDT, rather than between FDTs. Structure/string offsets remain.
    struct.pack_into('>I', result, 4, len(result))
    return bytes(result)


def bundle(data):
    result = []
    position = 0
    while position < len(data):
        if not any(data[position:]):
            break
        if len(data) - position < 40:
            raise ValueError('Truncated DTB bundle')
        size = struct.unpack_from('>I', data, position + 4)[0]
        if size < 40 or position + size > len(data):
            raise ValueError('Invalid DTB size')
        result.append(aligned(data[position:position + size]))
        position += size
    if not result:
        raise ValueError('Empty DTB bundle')
    return b''.join(result)


if __name__ == '__main__':
    if len(sys.argv) < 3:
        raise SystemExit('Usage: pack-dtbs.py OUTPUT DTB...')
    payload = b''.join(aligned(Path(name).read_bytes()) for name in sys.argv[2:])
    Path(sys.argv[1]).write_bytes(payload)
