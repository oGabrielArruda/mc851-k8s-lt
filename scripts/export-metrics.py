#!/usr/bin/env python3
"""Exporta as séries de uma rodada do Prometheus para CSV.

Arquivos gerados em --out:
  timeseries.csv  uma linha a cada STEP s; t = segundos desde o início do k6
  cpu_pods.csv    CPU por pod (formato longo: t, pod, cores)
  pods.csv        ciclo de vida de cada pod (criado, pronto, deleção), em t relativo
"""
import argparse
import csv
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path

STEP = 5
NS = 'namespace="hpa-lab"'
HPA = f'{NS},horizontalpodautoscaler="fib-server"'
APP = f'{NS},container="fib-server"'


def series(testid):
    k6 = f'testid="{testid}"'
    return {
        # carga e falhas vistas pelo cliente
        "k6_rps": f"sum(rate(k6_http_reqs_total{{{k6}}}[10s]))",
        "k6_fail_rps": f'sum(rate(k6_http_reqs_total{{{k6},expected_response="false"}}[10s])) or vector(0)',
        "k6_dropped_rps": f"sum(rate(k6_dropped_iterations_total{{{k6}}}[10s])) or vector(0)",
        "k6_p50": f"histogram_quantile(0.50, sum(rate(k6_http_req_duration_seconds{{{k6}}}[10s])))",
        "k6_p95": f"histogram_quantile(0.95, sum(rate(k6_http_req_duration_seconds{{{k6}}}[10s])))",
        "k6_p99": f"histogram_quantile(0.99, sum(rate(k6_http_req_duration_seconds{{{k6}}}[10s])))",
        "k6_vus": f"sum(k6_vus{{{k6}}})",
        # servidor
        "srv_rps_ok": f'sum(rate(fib_requests_total{{{NS},code="200"}}[10s])) or vector(0)',
        "srv_rps_499": f'sum(rate(fib_requests_total{{{NS},code="499"}}[10s])) or vector(0)',
        "srv_p95": f"histogram_quantile(0.95, sum by (le) (rate(fib_request_duration_seconds_bucket{{{NS}}}[15s])))",
        "srv_queue_p95": f"histogram_quantile(0.95, sum by (le) (rate(fib_queue_wait_seconds_bucket{{{NS}}}[15s])))",
        "srv_in_flight": f"sum(fib_requests_in_flight{{{NS}}})",
        # HPA e pods
        "hpa_desired": f"max(kube_horizontalpodautoscaler_status_desired_replicas{{{HPA}}})",
        "hpa_current": f"max(kube_horizontalpodautoscaler_status_current_replicas{{{HPA}}})",
        "hpa_util": f'max(kube_horizontalpodautoscaler_status_target_metric{{{HPA},metric_target_type="utilization"}})',
        "hpa_target": f'max(kube_horizontalpodautoscaler_spec_target_metric{{{HPA},metric_target_type="utilization"}})',
        "pods_ready": f'sum(kube_pod_status_ready{{{NS},pod=~"fib-server-.*",condition="true"}})',
        "pods_total": f'count(kube_pod_created{{{NS},pod=~"fib-server-.*"}})',
        # cAdvisor atualiza a cada ~10-15 s: janela de 30 s garante 2+ amostras
        "cpu_total": f"sum(rate(container_cpu_usage_seconds_total{{{APP}}}[30s]))",
        "cpu_throttled_frac": f"sum(rate(container_cpu_cfs_throttled_periods_total{{{APP}}}[30s])) "
                              f"/ sum(rate(container_cpu_cfs_periods_total{{{APP}}}[30s]))",
        "restarts": f"sum(kube_pod_container_status_restarts_total{{{NS}}})",
        "node_cpu": 'sum(rate(node_cpu_seconds_total{mode!="idle"}[15s]))',
    }


def query_range(prom, expr, start, end):
    qs = urllib.parse.urlencode({"query": expr, "start": start, "end": end, "step": STEP})
    with urllib.request.urlopen(f"{prom}/api/v1/query_range?{qs}", timeout=60) as r:
        data = json.load(r)
    if data["status"] != "success":
        raise RuntimeError(f"{expr}: {data}")
    return data["data"]["result"]


def to_float(v):
    f = float(v)
    return "" if math.isnan(f) or math.isinf(f) else round(f, 6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prom", required=True)
    ap.add_argument("--testid", required=True)
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pre", type=float, default=30, help="segundos antes do início a incluir")
    ap.add_argument("--post", type=float, default=20, help="segundos após o fim a incluir")
    a = ap.parse_args()
    out = Path(a.out)
    t0 = a.start
    start = math.floor(a.start - a.pre)
    end = math.ceil(a.end + a.post)

    # ---- séries agregadas
    cols = series(a.testid)
    grid = {}
    for name, expr in cols.items():
        for res in query_range(a.prom, expr, start, end)[:1]:
            for ts, v in res["values"]:
                grid.setdefault(float(ts), {})[name] = to_float(v)
    with open(out / "timeseries.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "epoch", *cols])
        for ts in sorted(grid):
            w.writerow([round(ts - t0, 1), ts, *(grid[ts].get(c, "") for c in cols)])

    # ---- CPU por pod
    with open(out / "cpu_pods.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "pod", "cores"])
        expr = f"sum by (pod) (rate(container_cpu_usage_seconds_total{{{APP}}}[30s]))"
        for res in query_range(a.prom, expr, start, end):
            for ts, v in res["values"]:
                w.writerow([round(float(ts) - t0, 1), res["metric"]["pod"], to_float(v)])

    # ---- ciclo de vida dos pods (timestamps absolutos vindos do kube-state-metrics)
    pods = {}
    for key, metric in [("created", "kube_pod_created"),
                        ("ready", "kube_pod_status_ready_time"),
                        ("deleted", "kube_pod_deletion_timestamp")]:
        expr = f'max by (pod) ({metric}{{{NS},pod=~"fib-server-.*"}})'
        for res in query_range(a.prom, expr, start, end):
            vals = [float(v) for _, v in res["values"] if float(v) > 0]
            if vals:
                p = pods.setdefault(res["metric"]["pod"], {})
                # created/deleted: primeiro valor; ready: último (pode mudar se o pod oscilar)
                p[key] = vals[-1] if key == "ready" else vals[0]
    with open(out / "pods.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pod", "created_t", "ready_t", "deleted_t", "startup_s"])
        for pod, p in sorted(pods.items(), key=lambda kv: kv[1].get("created", 0)):
            rel = {k: (round(p[k] - t0, 1) if k in p else "") for k in ("created", "ready", "deleted")}
            startup = round(p["ready"] - p["created"], 1) if "ready" in p and "created" in p else ""
            w.writerow([pod, rel["created"], rel["ready"], rel["deleted"], startup])

    print(f"exportado: {len(grid)} pontos, {len(pods)} pods → {out}")


if __name__ == "__main__":
    main()
