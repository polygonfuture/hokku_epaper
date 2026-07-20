#!/bin/bash
# Build the E1003 firmware and produce a merged single-file binary.
# Run inside an ESP-IDF environment (idf.py + esptool.py must be on PATH).
# Mirror of ../firmware/ci-build.sh with E1003 naming — the merged file is
# deliberately named hokku-firmware-e1003_* (NOT hokku-firmware_*) so the
# Spectra-6 setup tool's release matcher can never pick it up and flash it
# onto a frame (and vice versa). See README.md "Never cross-flash".
set -e

idf.py reconfigure build

VERSION=$(cat VERSION)

if [ -z "$VERSION" ]; then
    echo "ERROR: firmware-e1003/VERSION is empty"
    exit 1
fi
echo "Version: $VERSION"

mkdir -p release
esptool.py --chip esp32s3 merge_bin \
    --output release/hokku-firmware-e1003_${VERSION}.bin \
    0x0     build/bootloader/bootloader.bin \
    0x8000  build/partition_table/partition-table.bin \
    0x10000 build/hokku_epaper_e1003.bin

echo "Merged: release/hokku-firmware-e1003_${VERSION}.bin"
