#!/bin/sh
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CAPTURE=${CLAUDE_TURN_HEADERS_FILE:-"$PWD/claude-turn-headers.jsonl"}

if [ "$#" -eq 0 ]; then
    echo "usage: $0 claude [arguments ...]" >&2
    echo "This script does not start Claude without an explicit command." >&2
    exit 2
fi

umask 077
: >"$CAPTURE"

if [ -n "${BUN_OPTIONS:-}" ]; then
    BUN_OPTIONS="$BUN_OPTIONS --preload=$HERE/preload.cjs"
else
    BUN_OPTIONS="--preload=$HERE/preload.cjs"
fi

export BUN_OPTIONS
export CLAUDE_TURN_HEADERS_FILE="$CAPTURE"

printf 'turn headers -> %s\n' "$CAPTURE" >&2
exec "$@"
