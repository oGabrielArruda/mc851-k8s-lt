#!/usr/bin/env bash
# Instala Prometheus + Grafana (kube-prometheus-stack), o ServiceMonitor da app
# e o dashboard do laboratório.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="${PROFILE:-hpa}"
CHART_VERSION="${CHART_VERSION:-93.10.0}"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null

helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --version "$CHART_VERSION" \
  --namespace monitoring --create-namespace \
  -f "$ROOT/observability/values.yaml" \
  --wait --timeout 10m

kubectl apply -f "$ROOT/observability/servicemonitor.yaml"

python3 "$ROOT/observability/build-dashboard.py"
kubectl -n monitoring create configmap hpa-lab-dashboard \
  --from-file=hpa-lab.json="$ROOT/observability/dashboards/hpa-lab.json" \
  --dry-run=client -o yaml \
  | kubectl label --local -f - grafana_dashboard=1 -o yaml \
  | kubectl apply -f -

IP="$(minikube -p "$PROFILE" ip)"
echo ">> Prometheus: http://$IP:30090"
echo ">> Grafana:    http://$IP:30030  (admin/admin) → dashboard 'HPA Lab — fib-server'"
echo ">> k6 remote write: http://$IP:30090/api/v1/write"
