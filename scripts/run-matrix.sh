#!/usr/bin/env bash
# Executa a matriz de experimentos. Retomável: rodadas com results/<id>/DONE
# são puladas (útil se a máquina reiniciar no meio).
#
# A ordem intercala as repetições (todas as rodadas da rep 1, depois rep 2...)
# para que variações do host ao longo do tempo não se concentrem numa config.
#
# Uso: scripts/run-matrix.sh [filtro-regex]     ex.: scripts/run-matrix.sh 'spike_baseline'
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RESULTS="${RESULTS:-$ROOT/results}"
REPS="${REPS:-3}"
FILTER="${1:-.}"
mkdir -p "$RESULTS"

MATRIX=(
  "spike baseline"  "stress baseline"  "wave baseline"
  "spike sync5s"    "wave sync5s"
  "wave stab60"     "wave stab0"
  "spike target80"  "stress target80"
  "spike res60s"
)

for rep in $(seq 1 "$REPS"); do
  for entry in "${MATRIX[@]}"; do
    read -r scenario config <<<"$entry"
    id="${scenario}_${config}_r${rep}"
    [[ "$id" =~ $FILTER ]] || continue
    if [ -f "$RESULTS/$id/DONE" ]; then echo "pulando $id (já concluído)"; continue; fi
    rm -rf "${RESULTS:?}/$id"
    "$ROOT/scripts/run-experiment.sh" "$scenario" "$config" "$rep" \
      || echo "[$(date +%H:%M:%S)] FALHOU: $id (seguindo para a próxima)"
  done
done 2>&1 | tee -a "$RESULTS/matrix.log"
