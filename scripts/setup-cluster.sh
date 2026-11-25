#!/usr/bin/env bash
# Sobe o cluster Minikube com recursos fixos (reprodutibilidade), habilita o
# metrics-server, builda a imagem da app e aplica os manifests.
#
# Uso: scripts/setup-cluster.sh [args extras do minikube start]
#   ex.: scripts/setup-cluster.sh --extra-config=controller-manager.horizontal-pod-autoscaler-sync-period=5s
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="${PROFILE:-hpa}"
CPUS="${CPUS:-6}"
MEMORY="${MEMORY:-8192}"
K8S_VERSION="${K8S_VERSION:-v1.37.0}"
IMAGE="fib-server:v1"

minikube start -p "$PROFILE" \
  --driver=docker \
  --cpus="$CPUS" --memory="$MEMORY" \
  --kubernetes-version="$K8S_VERSION" \
  "$@"

# Com o driver docker, o minikube registra --cpus mas não limita a CPU do
# container do nó (só a memória). Aplicamos o limite diretamente no Docker.
# Obs.: o kubelet continua reportando a CPU do host como capacidade do nó.
docker update --cpus="$CPUS" "$PROFILE" >/dev/null

minikube -p "$PROFILE" addons enable metrics-server
# O addon do minikube usa --metric-resolution=60s; o padrão upstream do
# metrics-server é 15s. Usamos o padrão upstream (variável: METRIC_RESOLUTION).
METRIC_RESOLUTION="${METRIC_RESOLUTION:-15s}"
kubectl -n kube-system get deploy metrics-server -o json \
  | python3 -c 'import json,sys,os
d=json.load(sys.stdin); c=d["spec"]["template"]["spec"]["containers"][0]
c["args"]=[a for a in c["args"] if not a.startswith("--metric-resolution")]+["--metric-resolution="+os.environ["R"]]
print(json.dumps(d))' \
  | R="$METRIC_RESOLUTION" kubectl apply -f - >/dev/null
kubectl -n kube-system rollout status deploy/metrics-server --timeout=120s

echo ">> buildando $IMAGE e carregando no minikube"
docker build -t "$IMAGE" "$ROOT/app"
minikube -p "$PROFILE" image load "$IMAGE" --overwrite
minikube -p "$PROFILE" image ls | grep -q "fib-server:v1" || { echo "imagem não carregada"; exit 1; }

kubectl apply -f "$ROOT/k8s/namespace.yaml"
kubectl apply -f "$ROOT/k8s/deployment.yaml" -f "$ROOT/k8s/service.yaml"
kubectl apply -f "$ROOT/k8s/hpa-baseline.yaml"

kubectl -n hpa-lab rollout status deploy/fib-server --timeout=120s
echo ">> app acessível em: http://$(minikube -p "$PROFILE" ip):30080/fib"
