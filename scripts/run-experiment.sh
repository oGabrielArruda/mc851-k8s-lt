#!/usr/bin/env bash
# Executa UMA rodada de experimento de forma reprodutível:
#   1. garante a configuração (HPA, sync period do controlador, resolução do metrics-server)
#   2. reseta a app para 1 réplica ociosa
#   3. roda o cenário do k6
#   4. exporta métricas, eventos e metadados para results/<id>/
#
# Uso: scripts/run-experiment.sh <spike|stress|wave> <config> <rep>
# Configs: baseline | sync5s | stab60 | stab0 | target80 | res60s
# Variáveis: TIME_SCALE (padrão 1), RESULTS (padrão results/)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCENARIO="${1:?cenário}"; CONFIG="${2:?config}"; REP="${3:?repetição}"
PROFILE="${PROFILE:-hpa}"
TIME_SCALE="${TIME_SCALE:-1}"
RESULTS="${RESULTS:-$ROOT/results}"
NS=hpa-lab
IP="$(minikube -p "$PROFILE" ip)"
PROM="http://$IP:30090"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# ---------------------------------------------------------------- configuração
HPA_MANIFEST="$ROOT/k8s/hpa-baseline.yaml"; SYNC=15s; RES=15s
case "$CONFIG" in
  baseline) ;;
  sync5s)   SYNC=5s ;;
  stab60)   HPA_MANIFEST="$ROOT/k8s/hpa-stab60.yaml" ;;
  stab0)    HPA_MANIFEST="$ROOT/k8s/hpa-stab0.yaml" ;;
  target80) HPA_MANIFEST="$ROOT/k8s/hpa-target80.yaml" ;;
  res60s)   RES=60s ;;
  *) echo "config desconhecida: $CONFIG" >&2; exit 1 ;;
esac

ID="${SCENARIO}_${CONFIG}_r${REP}"
OUT="$RESULTS/$ID"
mkdir -p "$OUT"

ensure_metric_resolution() {
  local cur
  cur=$(kubectl -n kube-system get deploy metrics-server \
        -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -o 'metric-resolution=[0-9a-z]*' || true)
  if [ "$cur" != "metric-resolution=$1" ]; then
    log "metrics-server: $cur → $1"
    kubectl -n kube-system get deploy metrics-server -o json | R="$1" python3 -c 'import json,sys,os
d=json.load(sys.stdin); c=d["spec"]["template"]["spec"]["containers"][0]
c["args"]=[a for a in c["args"] if not a.startswith("--metric-resolution")]+["--metric-resolution="+os.environ["R"]]
print(json.dumps(d))' | kubectl apply -f - >/dev/null
    kubectl -n kube-system rollout status deploy/metrics-server --timeout=180s >/dev/null
  fi
}

ensure_sync_period() {
  local manifest=/etc/kubernetes/manifests/kube-controller-manager.yaml cur
  cur=$(minikube -p "$PROFILE" ssh "sudo grep -o 'horizontal-pod-autoscaler-sync-period=[0-9a-z]*' $manifest || true" | tr -d '\r')
  [ "$cur" = "horizontal-pod-autoscaler-sync-period=$1" ] && return
  log "controller-manager sync period: ${cur:-padrão} → $1 (reinicia o controlador)"
  minikube -p "$PROFILE" ssh "sudo sed -i '/horizontal-pod-autoscaler-sync-period/d' $manifest && \
    sudo sed -i 's|^\(\s*\)- kube-controller-manager$|&\n\1- --horizontal-pod-autoscaler-sync-period=$1|' $manifest" >/dev/null
  # o kubelet recria o static pod; espera o novo processo estar com a flag e pronto
  until kubectl -n kube-system get pod "kube-controller-manager-$PROFILE" \
          -o jsonpath='{.spec.containers[0].command}' 2>/dev/null | grep -q "sync-period=$1" \
        && kubectl -n kube-system wait --for=condition=Ready "pod/kube-controller-manager-$PROFILE" --timeout=5s >/dev/null 2>&1; do
    sleep 3
  done
  sleep 10 # eleição de líder / informers
}

reset_app() {
  log "reset: removendo HPA e voltando para 1 réplica"
  kubectl -n $NS delete hpa fib-server --ignore-not-found >/dev/null
  kubectl -n $NS scale deploy/fib-server --replicas=1 >/dev/null
  until [ "$(kubectl -n $NS get pods -l app=fib-server --no-headers 2>/dev/null | wc -l)" = 1 ]; do sleep 2; done
  kubectl -n $NS rollout status deploy/fib-server --timeout=180s >/dev/null
  kubectl apply -f "$HPA_MANIFEST" >/dev/null
  log "aguardando HPA estável (métrica disponível, 1 réplica, CPU < 20%)"
  local ok=0 deadline=$(( $(date +%s) + 600 ))
  while [ $ok -lt 3 ]; do
    [ "$(date +%s)" -gt "$deadline" ] && { log "ERRO: HPA não estabilizou"; exit 1; }
    read -r util cur < <(kubectl -n $NS get hpa fib-server \
      -o jsonpath='{.status.currentMetrics[0].resource.current.averageUtilization} {.status.currentReplicas}{"\n"}' 2>/dev/null || echo "x x")
    if [[ "$util" =~ ^[0-9]+$ ]] && [ "$util" -lt 20 ] && [ "$cur" = 1 ]; then ok=$((ok+1)); else ok=0; fi
    sleep 5
  done
}

# -------------------------------------------------------------------- execução
log "=== $ID (HPA=$(basename "$HPA_MANIFEST"), sync=$SYNC, metric-resolution=$RES, TIME_SCALE=$TIME_SCALE)"
ensure_metric_resolution "$RES"
ensure_sync_period "$SYNC"
reset_app
sleep 15 # linha de base nas métricas

TESTID="${ID}_$(date +%Y%m%d-%H%M%S)"
START=$(date +%s.%N)
log "k6 iniciando (testid=$TESTID)"
set +e
TIME_SCALE="$TIME_SCALE" "$ROOT/load/run.sh" "$SCENARIO" "$TESTID" \
  -e TIME_SCALE="$TIME_SCALE" -e SUMMARY="$OUT/k6-summary.json" > "$OUT/k6-stdout.txt" 2> "$OUT/k6-stderr.txt"
K6_EXIT=$?
set -e
END=$(date +%s.%N)
log "k6 terminou (exit=$K6_EXIT)"
sleep 20 # última coleta/push

# ---------------------------------------------------------------- exportação
python3 "$ROOT/scripts/export-metrics.py" --prom "$PROM" --testid "$TESTID" \
  --start "$START" --end "$END" --out "$OUT"
kubectl -n $NS get events -o json > "$OUT/events.json"
grep -oE 'refused|timeout|reset by peer|EOF' "$OUT/k6-stderr.txt" | sort | uniq -c > "$OUT/k6-errors.txt" || true
# o stderr do k6 pode ter centenas de MB sob saturação; guarda só uma amostra
head -n 200 "$OUT/k6-stderr.txt" > "$OUT/k6-stderr-head.txt" && rm -f "$OUT/k6-stderr.txt"

python3 - "$OUT/metadata.json" <<EOF
import json, subprocess, sys
def sh(c):
    try: return subprocess.check_output(c, shell=True, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception: return None
json.dump({
  "id": "$ID", "scenario": "$SCENARIO", "config": "$CONFIG", "rep": $REP,
  "testid": "$TESTID", "start": $START, "end": $END, "k6_exit": $K6_EXIT,
  "time_scale": $TIME_SCALE,
  "knobs": {"hpa_manifest": "$(basename "$HPA_MANIFEST")", "hpa_sync_period": "$SYNC",
            "metric_resolution": "$RES"},
  "git_commit": sh("git -C '$ROOT' rev-parse --short HEAD"),
  "git_dirty": bool(sh("git -C '$ROOT' status --porcelain -- app k8s load scripts observability")),
  "versions": {"k8s": sh("kubectl version -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)[\"serverVersion\"][\"gitVersion\"])'"),
               "minikube": sh("minikube version --short"), "k6": sh("k6 version | head -1")},
}, open(sys.argv[1], "w"), indent=2)
EOF
touch "$OUT/DONE"
log "=== $ID concluído → $OUT"
