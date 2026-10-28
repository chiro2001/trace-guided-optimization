#!/bin/sh
# Use as PY in run_real_test_official.sh; all profiling runs inside the container.
set -eu
: "${TGO_APP:?set TGO_APP to AppDir/AppRun or extracted AppImage entry}"
: "${TGO_RUN:?set a new capture directory}"
: "${TGO_PYTHON:=/usr/local/python3.12.13/bin/python3}"
case "${TGO_CONTAINER_ACTION:-record}" in
record)
    exec "$TGO_APP" record -o "$TGO_RUN" --backend "${TGO_BACKEND:-perf}" \
        --roi marker --frequency "${TGO_FREQUENCY:-9999}" --timeout 600 \
        --env "LD_PRELOAD=${LD_PRELOAD-}" --env "LD_LIBRARY_PATH=${LD_LIBRARY_PATH-}" \
        --env "PYTHONPATH=${PYTHONPATH-}" -- "$TGO_PYTHON" "$@"
    ;;
optimize|tune)
    : "${TGO_BUNDLE:?set a new output bundle directory}"
    TGO_DEFAULT_PAIRS=3
    if [ "$TGO_CONTAINER_ACTION" = tune ]; then TGO_DEFAULT_PAIRS=5; fi
    exec "$TGO_APP" "$TGO_CONTAINER_ACTION" --run "$TGO_RUN" -o "$TGO_BUNDLE" \
        --image "${TGO_IMAGE_FILTER:-libpython3.12.so.1.0}" \
        --metric decode --validate "${TGO_VALIDATE_PAIRS:-$TGO_DEFAULT_PAIRS}" --timeout 600
    ;;
*) echo "unknown TGO_CONTAINER_ACTION" >&2; exit 2 ;;
esac
