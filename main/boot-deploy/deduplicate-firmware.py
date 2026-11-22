#!/usr/bin/env python3
"""Deduplicate identical initramfs firmware without changing its logical files."""

import gzip
import hashlib
import json
import lzma
import mmap
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


def entries(data):
    result = []
    offset = 0
    while offset + 110 <= len(data):
        start = offset
        if data[offset:offset + 6] != b'070701':
            raise ValueError('Expected a newc initramfs')
        fields = [int(data[offset + 6 + i * 8:offset + 14 + i * 8], 16) for i in range(13)]
        namesize, size = fields[11], fields[6]
        if namesize < 1 or fields[12] != 0:
            raise ValueError('Invalid newc header')
        name_start = offset + 110
        name_end = name_start + namesize
        if name_end > len(data) or data[name_end - 1] != 0:
            raise ValueError('Invalid newc filename')
        name = data[name_start:name_end - 1].decode('utf-8', 'surrogateescape')
        payload = (name_end + 3) & ~3
        end = (payload + size + 3) & ~3
        if end > len(data):
            raise ValueError('Truncated newc file')
        result.append((name, fields, start, payload, end))
        offset = end
        if name == 'TRAILER!!!':
            if any(data[offset:]):
                raise ValueError('Concatenated initramfs archives are not supported')
            return result
    raise ValueError('Missing newc trailer')


def payload_hash(data, entry):
    view = memoryview(data)[entry[3]:entry[3] + entry[1][6]]
    try:
        return hashlib.sha256(view).hexdigest()
    finally:
        view.release()


def file_hash(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(data)
    return checksum.digest()


def logical_files(data, records):
    links = {}
    for entry in records:
        fields = entry[1]
        if stat.S_ISREG(fields[1]) and fields[4] > 1 and fields[6]:
            key = fields[0], fields[7], fields[8]
            digest = payload_hash(data, entry)
            if key in links and links[key] != digest:
                raise ValueError('Conflicting hardlink contents')
            links[key] = digest
    result = {}
    for entry in records:
        name, fields = entry[:2]
        digest = payload_hash(data, entry)
        if stat.S_ISREG(fields[1]) and fields[4] > 1:
            digest = links.get((fields[0], fields[7], fields[8]), digest)
        result[name] = tuple(fields[i] for i in (1, 2, 3, 5, 7, 8, 9, 10)), digest
    return result


def deduplicate_newc(source, destination):
    with source.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        records = entries(data)
        before = logical_files(data, records)
        groups = {}
        for index, entry in enumerate(records):
            name, fields = entry[:2]
            normalized = name.removeprefix('./').lstrip('/')
            if (stat.S_ISREG(fields[1]) and fields[4] <= 1 and fields[6]
                    and normalized.startswith(('usr/lib/firmware/', 'lib/firmware/'))):
                key = tuple(fields[i] for i in (1, 2, 3, 5, 7, 8, 9, 10)), fields[6], payload_hash(data, entry)
                groups.setdefault(key, []).append(index)
        duplicates = [group for group in groups.values() if len(group) > 1]
        if not duplicates:
            return {'groups': 0, 'saved_raw_bytes': 0, 'files': len(records) - 1}
        next_inode = max(entry[1][0] for entry in records) + 1
        replacements = {}
        saved = 0
        for group in duplicates:
            if next_inode > 0xffffffff:
                raise ValueError('No free newc inode')
            for index in group:
                replacements[index] = next_inode, len(group), index == group[-1]
            saved += records[group[0]][1][6] * (len(group) - 1)
            next_inode += 1
        with destination.open('wb') as output:
            for index, entry in enumerate(records):
                name, fields, start, payload, end = entry
                if index not in replacements:
                    output.write(data[start:end])
                    continue
                updated = fields.copy()
                updated[0], updated[4], keep_data = replacements[index]
                updated[6] = fields[6] if keep_data else 0
                header = b'070701' + b''.join(f'{value:08x}'.encode() for value in updated)
                filename = data[start + 110:start + 110 + fields[11]]
                output.write(header + filename)
                output.write(b'\0' * (-(len(header) + len(filename)) % 4))
                if keep_data:
                    output.write(data[payload:payload + fields[6]])
                    output.write(b'\0' * (-fields[6] % 4))
            output.write(b'\0' * (-output.tell() % 512))
        with destination.open('rb') as stream2, mmap.mmap(stream2.fileno(), 0, access=mmap.ACCESS_READ) as after:
            if before != logical_files(after, entries(after)):
                raise ValueError('Initramfs logical contents changed during deduplication')
        return {'groups': len(duplicates), 'saved_raw_bytes': saved, 'files': len(records) - 1}


def decompress(source, destination):
    with source.open('rb') as stream:
        magic = stream.read(6)
    if magic[:4] == b'\x28\xb5\x2f\xfd':
        with destination.open('wb') as output:
            subprocess.run(['zstd', '-q', '-d', '-c', str(source)], stdout=output, check=True)
        return 'zstd'
    if magic[:2] == b'\x1f\x8b':
        with gzip.open(source, 'rb') as stream, destination.open('wb') as output:
            shutil.copyfileobj(stream, output)
        return 'gzip'
    if magic == b'\xfd7zXZ\x00':
        with lzma.open(source, 'rb') as stream, destination.open('wb') as output:
            shutil.copyfileobj(stream, output)
        return 'xz'
    if magic == b'070701':
        shutil.copyfile(source, destination)
        return 'newc'
    return None


def compress(source, destination, codec):
    if codec == 'zstd':
        with destination.open('wb') as output:
            subprocess.run(['zstd', '-q', '-T0', '-3', '-c', str(source)], stdout=output, check=True)
    elif codec == 'gzip':
        with source.open('rb') as stream, destination.open('wb') as raw:
            with gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as output:
                shutil.copyfileobj(stream, output)
    elif codec == 'xz':
        with source.open('rb') as stream, lzma.open(destination, 'wb', check=lzma.CHECK_CRC32) as output:
            shutil.copyfileobj(stream, output)
    else:
        shutil.copyfile(source, destination)


def optimize(path):
    original = path.stat()
    with tempfile.TemporaryDirectory(prefix='.firmware-hardlinks-', dir=path.parent) as temporary:
        directory = Path(temporary)
        raw, deduped = directory / 'original.cpio', directory / 'deduped.cpio'
        codec = decompress(path, raw)
        if codec is None:
            return {'groups': 0, 'reason': 'unsupported compression', 'before_bytes': original.st_size, 'after_bytes': original.st_size}
        stats = deduplicate_newc(raw, deduped)
        stats.update(codec=codec, before_bytes=original.st_size, after_bytes=original.st_size)
        if not stats['groups']:
            return stats
        packed = directory / 'initramfs'
        compress(deduped, packed, codec)
        # Verify the compressed output as well as the newc hardlink semantics.
        verified = directory / 'verified.cpio'
        decompress(packed, verified)
        if file_hash(verified) != file_hash(deduped):
            raise ValueError('Compressed initramfs verification failed')
        stats['after_bytes'] = packed.stat().st_size
        if stats['after_bytes'] >= original.st_size:
            stats['reason'] = 'compressed image did not become smaller'
            stats['after_bytes'] = original.st_size
            return stats
        packed.chmod(stat.S_IMODE(original.st_mode))
        os.utime(packed, ns=(original.st_atime_ns, original.st_mtime_ns))
        packed.replace(path)
        return stats


if __name__ == '__main__':
    print('Initramfs firmware hardlinks: ' + json.dumps(optimize(Path(sys.argv[1])), sort_keys=True), flush=True)
