#!/usr/bin/env python3
"""Gera o dashboard Grafana do laboratório do HPA (dashboards/hpa-lab.json).

Todos os painéis compartilham a mesma linha do tempo para comparar carga,
latência, erros, CPU e réplicas. Rodar após editar: python3 build-dashboard.py
"""
import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}
NS = 'namespace="hpa-lab"'
W = "[$__rate_interval]"


def target(expr, legend, ref):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref, "range": True}


def panel(pid, title, x, y, exprs, unit="short", w=12, h=8, desc="", overrides=None, stack=False):
    return {
        "id": pid,
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(e, l, chr(65 + i)) for i, (e, l) in enumerate(exprs)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "lineWidth": 2,
                    "fillOpacity": 10 if stack else 0,
                    "showPoints": "never",
                    "stacking": {"mode": "normal" if stack else "none"},
                    "lineInterpolation": "stepAfter",
                },
            },
            "overrides": overrides or [],
        },
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["max", "mean"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def dashed(name):
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}],
    }


HPA = f'{NS},horizontalpodautoscaler="fib-server"'
APP = f'{NS},container="fib-server"'

panels = [
    panel(1, "Réplicas do HPA", 0, 0, [
        (f"kube_horizontalpodautoscaler_status_desired_replicas{{{HPA}}}", "desejadas"),
        (f"kube_horizontalpodautoscaler_status_current_replicas{{{HPA}}}", "atuais"),
        (f'sum(kube_pod_status_ready{{{NS},pod=~"fib-server-.*",condition="true"}})', "pods Ready"),
        (f"kube_horizontalpodautoscaler_spec_max_replicas{{{HPA}}}", "maxReplicas"),
    ], desc="Desejadas = decisão do controlador; Ready = pods de fato recebendo tráfego.",
        overrides=[dashed("maxReplicas")]),
    panel(2, "Utilização de CPU vista pelo HPA", 12, 0, [
        (f'kube_horizontalpodautoscaler_status_target_metric{{{HPA},metric_target_type="utilization"}}', "atual (média)"),
        (f'kube_horizontalpodautoscaler_spec_target_metric{{{HPA},metric_target_type="utilization"}}', "alvo"),
    ], unit="percent", desc="Valor que o HPA usa no cálculo: média de uso/request entre os pods.",
        overrides=[dashed("alvo")]),
    panel(3, "Throughput (req/s)", 0, 8, [
        (f"sum(rate(k6_http_reqs_total{W}))", "enviadas (k6)"),
        (f'sum by (code) (rate(fib_requests_total{{{NS}}}{W}))', "atendidas HTTP {{code}}"),
    ], unit="reqps"),
    panel(4, "Falhas (k6)", 12, 8, [
        # "or vector(0)": sem falhas a série não existe; mostra 0 em vez de vazio
        (f'sum(rate(k6_http_reqs_total{{expected_response="false"}}{W})) or vector(0)', "falhas/s"),
        (f"sum(rate(k6_dropped_iterations_total{W})) or vector(0)", "iterações descartadas/s"),
    ], unit="reqps", desc="Falhas = timeouts, conexões recusadas e status inesperados. "
                          "Iterações descartadas = k6 sem VUs livres (sistema saturado)."),
    panel(5, "Latência no servidor", 0, 16, [
        (f'histogram_quantile({q}, sum by (le) (rate(fib_request_duration_seconds_bucket{{{NS}}}{W})))', f"p{int(q * 100)}")
        for q in (0.5, 0.95, 0.99)
    ], unit="s", desc="Tempo de processamento dentro do pod (inclui espera por CPU throttled)."),
    panel(6, "Latência no cliente (k6)", 12, 16, [
        (f"histogram_quantile({q}, sum(rate(k6_http_req_duration_seconds{W})))", f"p{int(q * 100)}")
        for q in (0.5, 0.95, 0.99)
    ], unit="s", desc="Inclui rede, fila e conexão. Requisições que estouram o timeout contam como 5 s."),
    panel(7, "CPU por pod", 0, 24, [
        (f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{APP}}}{W}))', "{{pod}}"),
    ], unit="cores", stack=True, desc="Empilhado: a altura total é a CPU usada pela aplicação."),
    panel(8, "Throttling de CPU por pod", 12, 24, [
        (f'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{APP}}}{W})) '
         f'/ sum by (pod) (rate(container_cpu_cfs_periods_total{{{APP}}}{W}))', "{{pod}}"),
    ], unit="percentunit", desc="Fração dos períodos CFS em que o pod bateu no limit de CPU."),
    panel(9, "Requisições no servidor", 0, 32, [
        (f'sum(fib_requests_in_flight{{{NS}}}) - sum(fib_requests_running{{{NS}}})', "na fila"),
        (f'sum(fib_requests_running{{{NS}}})', "calculando"),
        (f'histogram_quantile(0.95, sum by (le) (rate(fib_queue_wait_seconds_bucket{{{NS}}}{W})))', "espera p95 (s)"),
    ], desc="Cada pod calcula no máximo MAX_CONCURRENCY requisições; as demais esperam na fila."),
    panel(10, "VUs do k6", 12, 32, [
        ("sum(k6_vus)", "VUs ativos"),
    ]),
]

dashboard = {
    "uid": "hpa-lab",
    "title": "HPA Lab — fib-server",
    "tags": ["mc851", "hpa"],
    "timezone": "browser",
    "refresh": "5s",
    "time": {"from": "now-15m", "to": "now"},
    "schemaVersion": 39,
    "panels": panels,
}

out = Path(__file__).parent / "dashboards" / "hpa-lab.json"
out.write_text(json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n")
print(f"gerado {out}")
