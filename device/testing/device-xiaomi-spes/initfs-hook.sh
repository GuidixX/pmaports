#!/bin/sh
# SPDX-License-Identifier: MIT
# Detection is diagnostic: it cannot fix a wrong DTB after the kernel starts.
if ! /usr/libexec/nura-device-variant; then
	echo "Nura: could not verify the active spes/spesn DTB; see the error above." >&2
fi
