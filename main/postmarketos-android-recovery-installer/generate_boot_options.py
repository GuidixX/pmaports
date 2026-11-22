#!/usr/bin/env python3
"""Generate recovery metadata from the actual Android boot image files."""

import hashlib
from pathlib import Path
import re
import shlex
import struct
import sys
import tarfile


def image_info(stream):
    checksum = hashlib.sha256()
    size = 0
    header = b""
    for data in iter(lambda: stream.read(1024 * 1024), b""):
        if not header:
            header = data[:44]
        size += len(data)
        checksum.update(data)
    return header, size, checksum.hexdigest()


def options(images):
    result = {'BOOT_INSTALL_SCHEMA': '1', 'VENDOR_BOOT_REQUIRED': 'false',
              'VENDOR_BOOT_PARTLABEL': 'vendor_boot'}
    boot = images.get('boot')
    if boot is None:
        return result
    header, size, checksum = boot
    if len(header) < 44 or header[:8] != b'ANDROID!':
        raise ValueError('Invalid Android boot image')
    version = struct.unpack_from('<I', header, 40)[0]
    if version not in (0, 1, 2, 3, 4):
        raise ValueError('Unsupported Android boot header')
    result.update(BOOT_HEADER_VERSION=str(version), BOOT_IMAGE_SIZE=str(size),
                  BOOT_IMAGE_SHA256=checksum)
    if version in (3, 4):
        vendor = images.get('vendor_boot')
        if vendor is None or vendor[0][:8] != b'VNDRBOOT':
            raise ValueError('Android header >=3 requires vendor_boot.img')
        if struct.unpack_from('<I', vendor[0], 8)[0] != version:
            raise ValueError('boot and vendor_boot header versions differ')
        result.update(VENDOR_BOOT_REQUIRED='true', VENDOR_BOOT_IMAGE_SIZE=str(vendor[1]),
                      VENDOR_BOOT_IMAGE_SHA256=vendor[2])
    return result


def from_directory(directory, flavor=''):
    images = {}
    for name in ('boot', 'vendor_boot'):
        path = directory / (name + '.img' + flavor)
        if path.is_file():
            with path.open('rb') as stream:
                images[name] = image_info(stream)
    return options(images)


def from_archive(archive, flavor=''):
    images = {}
    wanted = {'boot/' + name + '.img' + flavor: name for name in ('boot', 'vendor_boot')}
    with tarfile.open(archive, 'r|gz') as rootfs:
        for member in rootfs:
            name = member.name.removeprefix('./')
            if name in wanted and member.isfile():
                images[wanted[name]] = image_info(rootfs.extractfile(member))
    return options(images)


def serialize(values):
    return ''.join(name + '=' + shlex.quote(value) + '\n' for name, value in values.items())


def main():
    device = sys.argv[1]
    if not re.fullmatch(r'[a-z0-9-]+', device):
        raise ValueError('Invalid device name')
    config = Path('chroot/install_options').read_text()
    flavor = re.search(r"^FLAVOR='([^']*)'$", config, re.MULTILINE)
    flavor = flavor[1] if flavor else ''
    live = Path('/mnt/rootfs_' + device + '/boot')
    metadata = from_directory(live, flavor) if live.is_dir() else from_archive('rootfs.tar.gz', flavor)
    Path('chroot/boot_install_options').write_text(serialize(metadata))


if __name__ == '__main__':
    main()
