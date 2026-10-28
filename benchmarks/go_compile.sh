#!/bin/sh
# A fresh cache makes this a compile workload instead of a cache-hit benchmark.
set -eu
TGO_GO_CACHE=$(mktemp -d)
trap 'rm -rf "$TGO_GO_CACHE"' EXIT HUP INT TERM
GOCACHE="$TGO_GO_CACHE" go build -p 1 -o /dev/null cmd/compile
