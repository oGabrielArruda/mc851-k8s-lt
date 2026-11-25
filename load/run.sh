#!/usr/bin/env bash
# Roda um cenário do k6 enviando as métricas ao Prometheus do cluster.
#
# Uso: load/run.sh <spike|stress|wave> [testid] [args extras do k6]
#   ex.: TIME_SCALE=0.1 load/run.sh spike smoke
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SCENARIO="${1:?cenário: spike | stress | wave}"; shift
TESTID="${1:-${SCENARIO}-$(date +%Y%m%d-%H%M%S)}"; [ $# -gt 0 ] && shift
PROFILE="${PROFILE:-hpa}"
HOST="${HOST:-$(minikube -p "$PROFILE" ip)}"

export K6_PROMETHEUS_RW_SERVER_URL="http://$HOST:30090/api/v1/write"
export K6_PROMETHEUS_RW_TREND_STATS="p(50),p(95),p(99),avg,max"
export K6_PROMETHEUS_RW_PUSH_INTERVAL="5s"

exec k6 run --quiet --no-usage-report \
  -o experimental-prometheus-rw \
  --tag testid="$TESTID" \
  -e HOST="$HOST" \
  "$@" \
  "$DIR/$SCENARIO.js" 2> >(grep -v 'experimental-prometheus-rw output has been deprecated' >&2)
