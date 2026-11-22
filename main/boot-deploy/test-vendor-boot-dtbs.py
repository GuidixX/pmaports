#!/usr/bin/env python3
"""Check Android DTB packaging with the real mkbootimg; never flash anything."""

import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

FUNCTIONS = Path(sys.argv.pop(1)).resolve()
HARNESS = r'''
. "$1"
find_dtb() {
	[ -f "$TEST_DTB_DIR/$1.dtb" ] || return 1
	printf '%s\n' "$TEST_DTB_DIR/$1.dtb"
}
find_all_dtbs() {
	for name in $TEST_DTBS; do find_dtb "$name" || return 1; done
}
copy() { :; }
get_cmdline() { printf '%s\n' 'test=1'; }
require_package() { command -v "$1" >/dev/null; }
work_dir="$TEST_OUTPUT"
kernel_filename=vmlinuz
initfs_filename=initramfs
deviceinfo_generate_bootimg=true
deviceinfo_header_version="$TEST_HEADER"
deviceinfo_flash_pagesize=4096
deviceinfo_flash_offset_base="$TEST_BASE"
deviceinfo_flash_offset_dtb="$TEST_DTB_OFFSET"
deviceinfo_flash_offset_kernel="$TEST_KERNEL_OFFSET"
deviceinfo_flash_offset_ramdisk="$TEST_RAMDISK_OFFSET"
deviceinfo_flash_offset_second=0x00008000
deviceinfo_flash_offset_tags="$TEST_TAGS_OFFSET"
deviceinfo_bootimg_custom_args="$TEST_CUSTOM_ARGS"
deviceinfo_dtb="$TEST_DTBS"
append_or_copy_dtb
create_bootimg
'''


def dtb(board):
    strings = b"qcom,board-id\0"
    nodes = struct.pack(">I", 1) + b"\0" * 4
    nodes += struct.pack(">IIIII", 3, 8, 0, board, 0)
    nodes += struct.pack(">II", 2, 9)
    reserve = b"\0" * 16
    return (struct.pack(">10I", 0xd00dfeed, 56 + len(nodes) + len(strings),
                        56, 56 + len(nodes), 40, 17, 16, 0, len(strings), len(nodes))
            + reserve + nodes + strings)


class VendorBootTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "vmlinuz").write_bytes(b"test kernel")
        (self.root / "initramfs").write_bytes(b"test ramdisk")
        self.blobs = [dtb(0x40022), dtb(0x30022)]
        for name, data in zip(("spes", "spesn"), self.blobs):
            (self.root / (name + ".dtb")).write_bytes(data)

    def build(self, version, names, **settings):
        addresses = {"TEST_BASE": "0x00000000", "TEST_DTB_OFFSET": "0x01f00000",
                     "TEST_KERNEL_OFFSET": "0x00008000", "TEST_RAMDISK_OFFSET": "0x01000000",
                     "TEST_TAGS_OFFSET": "0x00000100", "TEST_CUSTOM_ARGS": ""}
        addresses.update(settings)
        return subprocess.run(["sh", "-c", HARNESS, "test", str(FUNCTIONS)],
                              env={**os.environ, **addresses,
                                   "BOOT_DEPLOY_PACK_DTBS": str(Path(__file__).resolve().with_name('pack-dtbs.py')),
                                   "BOOT_DEPLOY_FIRMWARE_DEDUP": str(Path(__file__).resolve().with_name('deduplicate-firmware.py')),
                                   "TEST_DTB_DIR": str(self.root),
                                   "TEST_OUTPUT": str(self.root), "TEST_HEADER": str(version),
                                   "TEST_DTBS": names}, capture_output=True, text=True)

    def assert_bundle(self, version, names, expected):
        result = self.build(version, names)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if version >= 3:
            data = (self.root / "vendor_boot.img").read_bytes()
            self.assertEqual(data[:8], b"VNDRBOOT")
            actual_version, page = struct.unpack_from("<II", data, 8)
            self.assertEqual(actual_version, version)
            ramdisk_size = struct.unpack_from("<I", data, 24)[0]
            header_size, dtb_size = struct.unpack_from("<II", data, 2096)
            align = lambda size: (size + page - 1) // page * page
            start = align(header_size) + align(ramdisk_size)
        else:
            data = (self.root / 'boot.img').read_bytes()
            page = struct.unpack_from('<I', data, 36)[0]
            align = lambda size: (size + page - 1) // page * page
            kernel_size = struct.unpack_from('<I', data, 8)[0]
            ramdisk_size = struct.unpack_from('<I', data, 16)[0]
            start = align(1660) + align(kernel_size) + align(ramdisk_size)
            dtb_size = struct.unpack_from('<I', data, 1648)[0]
            self.assertFalse((self.root / 'vendor_boot.img').exists())
        position, padded = 0, bytearray()
        while position < len(expected):
            size = struct.unpack_from('>I', expected, position + 4)[0]
            blob = bytearray(expected[position:position + size])
            position += size
            blob.extend(b'\0' * (-len(blob) % 8))
            struct.pack_into('>I', blob, 4, len(blob))
            padded.extend(blob)
        self.assertEqual(data[start:start + dtb_size], padded)
        position = 0
        while position < dtb_size:
            self.assertEqual((start + position) % 8, 0)
            size = struct.unpack_from('>I', data, start + position + 4)[0]
            self.assertEqual(size % 8, 0)
            position += size
        self.assertEqual(position, dtb_size)
        boot = (self.root / "boot.img").read_bytes()
        self.assertEqual(boot[:8], b"ANDROID!")
        self.assertEqual(struct.unpack_from("<I", boot, 40)[0], version)

    def test_header3_contains_both_dtbs_in_order(self):
        self.assert_bundle(3, "spes spesn", b"".join(self.blobs))

    def test_header2_contains_both_dtbs_in_order(self):
        self.assert_bundle(2, 'spes spesn', b''.join(self.blobs))

    def test_header4_contains_both_dtbs_in_order(self):
        self.assert_bundle(4, "spes spesn", b"".join(self.blobs))

    def test_single_dtb_still_works(self):
        self.assert_bundle(3, "spesn", self.blobs[1])

    def assert_addresses(self, version, expected, **settings):
        result = self.build(version, "spes spesn", **settings)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = (self.root / "vendor_boot.img").read_bytes()
        actual = (*struct.unpack_from("<II", data, 16),
                  struct.unpack_from("<I", data, 2076)[0],
                  struct.unpack_from("<Q", data, 2104)[0])
        self.assertEqual(actual, expected)

    def test_header3_preserves_explicit_zero_base(self):
        self.assert_addresses(3, (0x8000, 0x1000000, 0x100, 0x1f00000))

    def test_header4_adds_configured_base_to_each_offset(self):
        self.assert_addresses(4, (0x40080000, 0x46000000, 0x40001000, 0x4a000000),
                              TEST_BASE="0x40000000", TEST_KERNEL_OFFSET="0x80000",
                              TEST_RAMDISK_OFFSET="0x06000000", TEST_TAGS_OFFSET="0x1000",
                              TEST_DTB_OFFSET="0x0a000000")

    def test_unset_addresses_retain_mkbootimg_defaults(self):
        self.assert_addresses(3, (0x10008000, 0x11000000, 0x10000100, 0x11f00000),
                              **{name: "" for name in ("TEST_BASE", "TEST_KERNEL_OFFSET",
                                                       "TEST_RAMDISK_OFFSET", "TEST_TAGS_OFFSET",
                                                       "TEST_DTB_OFFSET")})

    def test_custom_args_can_override_standard_addresses(self):
        self.assert_addresses(3, (0x8000, 0x1000000, 0x100, 0x08000000),
                              TEST_CUSTOM_ARGS="--dtb_offset 0x08000000")

    def test_missing_second_dtb_aborts_before_creating_images(self):
        (self.root / "spesn.dtb").unlink()
        self.assertNotEqual(self.build(3, "spes spesn").returncode, 0)
        self.assertFalse((self.root / "boot.img").exists())
        self.assertFalse((self.root / "vendor_boot.img").exists())

    def test_single_dtb_header2_still_works(self):
        result = self.build(2, "spesn")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "boot.img").is_file())
        self.assertFalse((self.root / "vendor_boot.img").exists())


if __name__ == "__main__":
    unittest.main()
