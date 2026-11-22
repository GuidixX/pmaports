#!/usr/bin/env python3
"""Install the upstream executable and canonical mount helper symlinks."""

from pathlib import Path
import shutil
import sys
import tarfile


def install(apk, destination):
    with tarfile.open(apk, "r:*", ignore_zeros=True) as archive:
        metadata = archive.extractfile(".PKGINFO").read().decode()
        fields = dict(line.split(" = ", 1) for line in metadata.splitlines() if " = " in line)
        if (fields.get("pkgname"), fields.get("pkgver"), fields.get("arch")) != (
                "sshfs", "3.7.7-r0", "aarch64"):
            raise ValueError("Unexpected upstream SSHFS package")
        binary = archive.getmember("usr/bin/sshfs")
        if not binary.isfile() or binary.mode != 0o755:
            raise ValueError("Upstream SSHFS executable is missing or has unexpected permissions")
        directory = Path(destination) / "usr/bin"
        directory.mkdir(parents=True, exist_ok=True)
        with archive.extractfile(binary) as source, (directory / "sshfs").open("wb") as output:
            shutil.copyfileobj(source, output)
        (directory / "sshfs").chmod(binary.mode)
    for name in ("mount.sshfs", "mount.fuse.sshfs"):
        (directory / name).symlink_to("sshfs")


if __name__ == "__main__":
    install(sys.argv[1], sys.argv[2])
