#!/bin/sh
# Run the checkout with its private SDK, preserving workload env via --env.
set -eu
TGO_SOURCE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
unset LD_PRELOAD LD_AUDIT LD_LIBRARY_PATH PYTHONHOME PYTHONPATH
unset TGO_RUNTIME_DIR TGO_TOOL_ROOT TGO_PACKAGED TGO_APPDIR
export PYTHONPATH="$TGO_SOURCE_ROOT"
exec "${TGO_CONTROLLER_PYTHON:-$TGO_SOURCE_ROOT/deps/install/bin/python3.12}" -m tgo "$@"
