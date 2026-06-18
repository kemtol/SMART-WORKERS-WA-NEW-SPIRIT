#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=bin/worker-common.sh
. bin/worker-common.sh

print_worker_status
