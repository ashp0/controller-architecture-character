#!/usr/bin/env bash
# Start The Architecture (body + viewer + brain). Extra flags pass through, e.g. ./run.sh --camera 1
cd "$(dirname "$0")"
exec .venv/bin/python -m architecture "$@"
