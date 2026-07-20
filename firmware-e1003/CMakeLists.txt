cmake_minimum_required(VERSION 3.16)

# Read firmware version from VERSION file (format: PROTOCOL.CONFIG.N)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/VERSION" _FW_VER_RAW)
string(STRIP "${_FW_VER_RAW}" FIRMWARE_VERSION)
string(REPLACE "." ";" _FW_VER_LIST "${FIRMWARE_VERSION}")
list(GET _FW_VER_LIST 0 FW_PROTOCOL)
list(GET _FW_VER_LIST 1 FW_CONFIG)
list(GET _FW_VER_LIST 2 FW_N)

set(PROJECT_VER "${FIRMWARE_VERSION}")

# Build timestamp kept as debug metadata embedded in the binary app descriptor
string(TIMESTAMP BUILD_TIMESTAMP "%Y%m%d%H%M%SZ" UTC)

# Generate version.h for use by firmware source files
configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/main/version.h.in"
    "${CMAKE_BINARY_DIR}/version.h"
)

set(IDF_TARGET "esp32s3")
set(EXTRA_COMPONENT_DIRS "")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# DELIBERATELY distinct from the Spectra-6 build's project(hokku_epaper):
# the binary is named hokku_epaper_e1003.bin so the two firmwares can never
# be confused at flash time. Flashing the Spectra-6 build onto an E1003 is
# electrically hazardous (its pin map drives the E1003's KEY0 button and the
# IT8951's HRDY output as push-pull outputs). See README.md.
project(hokku_epaper_e1003)
