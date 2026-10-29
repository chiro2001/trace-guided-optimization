#!/bin/sh
# Use as PY in run_real_test_official.sh; profiling stays inside the container.
set -eu
: "${TGO_APP:?set TGO_APP to AppImage or AppDir/AppRun}"
: "${TGO_PYTHON:=/usr/local/python3.12.13/bin/python3}"
TGO_ACTION=${TGO_CONTAINER_ACTION:-record}
case "$TGO_ACTION" in
record)
    : "${TGO_RUN:?set a new capture directory}"
    set -- -- "$TGO_PYTHON" "$@"
    if [ -n "${TGO_EVENT-}" ]; then set -- --event "$TGO_EVENT" "$@"; fi
    exec "$TGO_APP" record -o "$TGO_RUN" --backend "${TGO_BACKEND:-auto}" \
        --roi marker --frequency "${TGO_FREQUENCY:-9999}" --timeout "${TGO_TIMEOUT:-600}" \
        --env "LD_PRELOAD=${LD_PRELOAD-}" --env "LD_LIBRARY_PATH=${LD_LIBRARY_PATH-}" \
        --env "PYTHONPATH=${PYTHONPATH-}" "$@"
    ;;
optimize|tune)
    : "${TGO_BUNDLE:?set a new output bundle directory}"
    TGO_DEFAULT_PAIRS=3
    if [ "$TGO_ACTION" = tune ]; then TGO_DEFAULT_PAIRS=5; fi
    if [ -n "${TGO_RUN-}" ]; then
        set -- --run "$TGO_RUN"
    elif [ "$TGO_ACTION" = tune ]; then
        set -- --backend "${TGO_BACKEND:-auto}" --roi marker --frequency "${TGO_FREQUENCY:-9999}" \
            --env "LD_PRELOAD=${LD_PRELOAD-}" --env "LD_LIBRARY_PATH=${LD_LIBRARY_PATH-}" \
            --env "PYTHONPATH=${PYTHONPATH-}" -- "$TGO_PYTHON" "$@"
        if [ -n "${TGO_EVENT-}" ]; then set -- --event "$TGO_EVENT" "$@"; fi
    else
        echo 'optimize requires TGO_RUN; tune can record a new capture when TGO_RUN is omitted' >&2
        exit 2
    fi
    if [ "$TGO_ACTION" = tune ] && [ -n "${TGO_LAYOUT-}" ]; then set -- --layout "$TGO_LAYOUT" "$@"; fi
    if [ "$TGO_ACTION" = tune ] && [ -n "${TGO_TRAIN_RUNS-}" ]; then set -- --train-runs "$TGO_TRAIN_RUNS" "$@"; fi
    if [ "$TGO_ACTION" = tune ] && [ -n "${TGO_IMAGE_SEARCH-}" ]; then set -- --image-search "$TGO_IMAGE_SEARCH" "$@"; fi
    if [ -n "${TGO_IMAGE_FILTER-}" ]; then
        TGO_PREVIOUS_IFS=$IFS
        IFS=,
        set -f
        for TGO_SELECTED_IMAGE in $TGO_IMAGE_FILTER; do
            set -- --image "$TGO_SELECTED_IMAGE" "$@"
        done
        IFS=$TGO_PREVIOUS_IFS
    fi
    if [ "${TGO_ALLOW_STRIPPED:-0}" = 1 ]; then set -- --allow-stripped "$@"; fi
    exec "$TGO_APP" "$TGO_ACTION" -o "$TGO_BUNDLE" \
        --top "${TGO_TOP:-3}" --bolt-mode "${TGO_BOLT_MODE:-auto}" \
        --entry-mode executable --metric decode --validate "${TGO_VALIDATE_PAIRS:-$TGO_DEFAULT_PAIRS}" \
        --timeout "${TGO_TIMEOUT:-600}" "$@"
    ;;
*) echo "unknown TGO_CONTAINER_ACTION" >&2; exit 2 ;;
esac
