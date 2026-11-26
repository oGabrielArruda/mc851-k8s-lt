#!/usr/bin/env python3
"""Análise da matriz de experimentos do HPA (Fase 6).

Lê results/<id>/ (somente rodadas com DONE) e gera:
  analysis/out/runs.csv        uma linha por rodada com as métricas definidas no plano
  analysis/out/summary.csv     média ± desvio por (cenário, config)
  analysis/out/summary.md      a mesma tabela em Markdown, para o relatório
  analysis/out/formula.csv     conferência da fórmula ceil(atual × uso/alvo) do controlador
  analysis/figs/*.png          gráficos

Uso: python3 analysis/analyze.py [--results results] [--rep 1]
  --rep escolhe qual repetição aparece nas figuras de linha do tempo.
"""
import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------------ parâmetros
# Perfis de carga (mesmos de load/*.js): taxa inicial + [taxa alvo, duração s]
PROFILES = {
    "spike": (5, [(5, 60), (60, 10), (60, 240), (5, 10), (5, 400)]),
    "stress": (5, [(420, 720), (420, 120), (5, 30), (5, 90)]),
    "wave": (10, [(10, 60), (70, 120), (10, 120), (70, 120), (10, 120),
                  (70, 360), (10, 360), (10, 360)]),
}
SPIKE_UP, SPIKE_DOWN = 60.0, 310.0     # início do salto e da queda no cenário de pico
CPU_REQUEST = 0.25                      # cores (requests do Deployment)
MAX_REPLICAS = 8
TOLERANCE = 0.10                        # tolerância padrão do controlador
TARGETS = {"target80": 80}              # demais configs usam 50%
SLOW_P95 = 0.5                          # s; limiar de "latência degradada"

CONFIG_ORDER = ["baseline", "sync5s", "res60s", "target80", "stab60", "stab0"]
CONFIG_LABEL = {
    "baseline": "baseline (sync 15 s, res. 15 s, alvo 50%, estab. 300 s)",
    "sync5s": "sync period 5 s",
    "res60s": "resolução do metrics-server 60 s",
    "target80": "alvo de CPU 80%",
    "stab60": "janela de estabilização 60 s",
    "stab0": "janela de estabilização 0 s",
}

# Paleta categórica (ordem fixa; validada pelo skill de dataviz)
SERIES = ["#257de0", "#f47230", "#00b894", "#f2a600", "#ee7fac", "#008a17"]
CONFIG_COLOR = {c: SERIES[i] for i, c in enumerate(CONFIG_ORDER)}
INK, INK2, GRID = "#1f1f1f", "#6b6b6b", "#e6e6e6"


def profile_rate(scenario, t):
    """Taxa oferecida (req/s) pelo perfil do k6 no instante t (interpolação linear)."""
    start, stages = PROFILES[scenario]
    knots_t, knots_r = [0.0], [float(start)]
    for r, d in stages:
        knots_t.append(knots_t[-1] + d)
        knots_r.append(float(r))
    return np.interp(t, knots_t, knots_r, right=np.nan)


# ------------------------------------------------------------------- leitura
def load_runs(results):
    runs = []
    for d in sorted(results.iterdir()):
        if not (d / "DONE").exists():
            continue
        meta = json.loads((d / "metadata.json").read_text())
        ts = pd.read_csv(d / "timeseries.csv")
        pods = pd.read_csv(d / "pods.csv")
        summ = json.loads((d / "k6-summary.json").read_text())
        runs.append({"dir": d, "meta": meta, "ts": ts, "pods": pods, "k6": summ["metrics"]})
    return runs


def first_t(ts, mask):
    hit = ts.loc[mask, "t"]
    return float(hit.iloc[0]) if len(hit) else math.nan


def integrate(t, y):
    y = np.nan_to_num(np.asarray(y, float))
    return float(np.trapezoid(y, t))


# ------------------------------------------------------------------- métricas
def cpu_per_request(runs):
    """Custo de CPU por requisição (core·s) do ponto de vista do HPA (metrics-server).

    Usa os trechos estáveis: todas as réplicas prontas, sem saturação e carga > 15 req/s.
    custo = uso médio × request × réplicas / vazão atendida (mediana).
    """
    vals = []
    for r in runs:
        ts = r["ts"]
        m = ((ts.hpa_current == ts.pods_ready) & (ts.hpa_util < 150)
             & (ts.srv_rps_ok > 15) & (ts.hpa_desired == ts.hpa_current))
        d = ts[m]
        vals += list(d.hpa_util / 100 * CPU_REQUEST * d.hpa_current / d.srv_rps_ok)
    return float(np.median(vals)) if vals else 0.0118


def run_metrics(r, cost):
    meta, ts, pods, k6 = r["meta"], r["ts"], r["pods"], r["k6"]
    sc, cfg = meta["scenario"], meta["config"]
    target = TARGETS.get(cfg, 50)
    dur = k6["http_reqs"]["values"]["count"]
    fails = k6.get("http_req_failed", {}).get("values", {}).get("rate", 0.0)
    lat = k6["http_req_duration"]["values"]
    dropped = k6.get("dropped_iterations", {}).get("values", {}).get("count", 0)
    run = ts[(ts.t >= 0) & (ts.t <= meta["end"] - meta["start"])]

    m = {
        "id": meta["id"], "scenario": sc, "config": cfg, "rep": meta["rep"],
        "requests": dur, "fail_pct": 100 * fails, "dropped": dropped,
        "p50_ms": lat["med"], "p95_ms": lat["p(95)"], "p99_ms": lat["p(99)"], "max_ms": lat["max"],
        "max_replicas": run.hpa_desired.max(),
        "pod_seconds": integrate(run.t, run.pods_ready),
        "scale_events": int((run.hpa_desired.diff().fillna(0) != 0).sum()),
        "restarts": run.restarts.max() - run.restarts.min(),
    }

    # réplicas "ideais": mínimo que mantém a utilização no alvo para a carga oferecida
    per_pod = target / 100 * CPU_REQUEST / cost
    offered = profile_rate(sc, run.t.to_numpy())
    ideal = np.clip(np.ceil(offered / per_pod), 1, MAX_REPLICAS)
    m["ideal_pod_seconds"] = integrate(run.t, ideal)
    over = np.clip(run.pods_ready.to_numpy() - ideal, 0, None)
    under = np.clip(ideal - run.pods_ready.to_numpy(), 0, None)
    m["over_pod_seconds"] = integrate(run.t, over)
    m["under_pod_seconds"] = integrate(run.t, under)
    m["overprov_pct"] = 100 * (m["pod_seconds"] / m["ideal_pod_seconds"] - 1)

    new = pods[pods.created_t >= 0]
    if sc == "spike":
        up = run[(run.t >= SPIKE_UP) & (run.t < SPIKE_DOWN)]
        peak = up.hpa_desired.max()
        # o Deployment cria os pods no mesmo instante em que o HPA muda a escala;
        # created_t vem do kube-state-metrics com resolução de 1 s
        m["detect_s"] = (new.created_t.min() - SPIKE_UP) if len(new) else math.nan
        m["first_ready_s"] = (new.ready_t.min() - SPIKE_UP) if len(new) else math.nan
        m["full_capacity_s"] = first_t(up, up.pods_ready >= peak) - SPIKE_UP
        m["reaction_p95_max_ms"] = 1000 * up.k6_p95.max()
        m["slow_s"] = 5 * int((up.k6_p95 > SLOW_P95).sum())       # passo de 5 s
        down = run[run.t >= SPIKE_DOWN]
        hi = down.hpa_desired.iloc[0]
        m["scaledown_start_s"] = first_t(down, down.hpa_desired < hi) - SPIKE_DOWN
        m["scaledown_min_s"] = first_t(down, down.hpa_desired <= 1) - SPIKE_DOWN
    if sc == "stress":
        sm = run.srv_rps_ok.rolling(6, min_periods=6).mean()     # 30 s
        m["max_served_rps"] = sm.max()
        m["t_max_replicas"] = first_t(run, run.hpa_desired >= MAX_REPLICAS)
        m["offered_at_max_replicas"] = float(profile_rate(sc, m["t_max_replicas"]))
        t_slow = first_t(run, run.k6_p95 > SLOW_P95)
        m["offered_at_slow"] = float(profile_rate(sc, t_slow))
        t_fail = first_t(run, run.k6_fail_rps > 1)
        m["offered_at_fail"] = float(profile_rate(sc, t_fail))
    return m


def formula_check(runs):
    """Compara réplicas desejadas com ceil(atual × uso/alvo), dentro da tolerância de 10%.

    Só pontos de scale-up: o scale-down é atrasado pela janela de estabilização
    e não segue a fórmula instantânea.

    Cada ponto é "estável" ou "pods novos". Em groupPods (replica_calculator.go),
    um pod com menos de 5 min (--horizontal-pod-autoscaler-cpu-initialization-period)
    só tem a métrica considerada se ela foi coletada pelo menos uma janela depois
    de o pod ficar Ready; antes disso ele é "unready" e, no scale-up, conta como 0%
    de uso, o que reduz (ou anula) o aumento. Como não temos o timestamp de cada
    coleta, aproximamos: pod é "novo" enquanto t < Ready + 2 × resolução + 30 s
    (janela da métrica + desalinhamento até a próxima coleta + housekeeping do
    cAdvisor + sync do HPA) — 60 s com resolução de 15 s, 150 s com 60 s.
    Só contam os pods que já existiam na decisão (criados até 5 s antes da amostra).
    """
    rows = []
    for r in runs:
        ts, cfg, pods = r["ts"], r["meta"]["config"], r["pods"]
        target = TARGETS.get(cfg, 50)
        res = float(r["meta"]["knobs"]["metric_resolution"].rstrip("s"))
        d = ts.dropna(subset=["hpa_util", "hpa_current", "hpa_desired"])
        d = d[d.hpa_current > 0]
        ratio = d.hpa_util / target
        # o status mostra o uso truncado para inteiro; 1e-9 evita erro de ponto flutuante na borda
        pred = np.where((ratio - 1).abs() <= TOLERANCE + 1e-9, d.hpa_current,
                        np.ceil(d.hpa_current * ratio))
        pred = np.clip(pred, 1, MAX_REPLICAS)
        # política padrão de scale-up: no máximo max(2×atual, atual+4) por período
        limit = np.maximum(2 * d.hpa_current, d.hpa_current + 4)
        pred = np.minimum(pred, limit)
        up = pred > d.hpa_current
        for (_, row), p in zip(d[up].iterrows(), pred[up]):
            alive = pods[(pods.created_t <= row.t - 5)
                         & (pods.deleted_t.isna() | (pods.deleted_t > row.t))]
            ready = alive.ready_t.fillna(alive.created_t)
            young = int((row.t < ready + 2 * res + 30).sum())
            youngest = row.t - ready.max() if len(ready) else math.nan
            rows.append({"id": r["meta"]["id"], "t": row.t, "current": row.hpa_current,
                         "util": row.hpa_util, "predicted": p, "desired": row.hpa_desired,
                         "young_pods": young, "youngest_ready_age_s": youngest,
                         "resolution_s": res,
                         "kind": "pods novos" if young else "estável"})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------- figuras
def style(ax):
    ax.grid(True, axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#bdbdbd")
    ax.tick_params(colors=INK2, labelsize=8)
    ax.yaxis.label.set_color(INK2)
    ax.xaxis.label.set_color(INK2)


def legend(ax, **kw):
    lg = ax.legend(frameon=False, fontsize=8, labelcolor=INK, **kw)
    return lg


def timeline(run, out, title):
    """Carga, réplicas e latência de uma rodada (painéis empilhados, eixo x comum)."""
    ts, sc = run["ts"], run["meta"]["scenario"]
    t = ts.t / 60
    fig, axes = plt.subplots(3, 1, figsize=(8, 7), sharex=True,
                             gridspec_kw={"height_ratios": [1.2, 1, 1]})
    a = axes[0]
    a.plot(t, profile_rate(sc, ts.t), color=INK2, lw=1.2, ls="--", label="oferecida (perfil k6)")
    a.plot(t, ts.srv_rps_ok, color=SERIES[0], lw=1.6, label="atendida (200)")
    if ts.k6_fail_rps.fillna(0).max() > 0.5:
        a.plot(t, ts.k6_fail_rps, color=SERIES[1], lw=1.6, label="falhas (timeout)")
    a.set_ylabel("req/s")
    legend(a, loc="upper right")
    a = axes[1]
    a.step(t, ts.hpa_desired, where="post", color=SERIES[1], lw=1.6, label="desejadas (HPA)")
    a.step(t, ts.pods_ready, where="post", color=SERIES[0], lw=1.6, label="prontas")
    a.set_ylabel("réplicas")
    a.set_ylim(0, MAX_REPLICAS + 0.8)
    legend(a, loc="upper right")
    a = axes[2]
    a.plot(t, 1000 * ts.k6_p50, color=SERIES[2], lw=1.6, label="p50")
    a.plot(t, 1000 * ts.k6_p95, color=SERIES[0], lw=1.6, label="p95")
    a.set_yscale("log")
    a.set_ylabel("latência no cliente (ms)")
    a.set_xlabel("tempo desde o início do teste (min)")
    legend(a, loc="upper right")
    for a in axes:
        style(a)
    fig.suptitle(title, x=0.01, ha="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def compare_replicas(runs, scenario, configs, rep, out, title, xmax=None):
    """Réplicas prontas ao longo do tempo para várias configs (mesma repetição)."""
    sel = {r["meta"]["config"]: r for r in runs
           if r["meta"]["scenario"] == scenario and r["meta"]["rep"] == rep}
    configs = [c for c in configs if c in sel]
    if not configs:
        return
    fig, axes = plt.subplots(2, 1, figsize=(8, 5.5), sharex=True,
                             gridspec_kw={"height_ratios": [0.6, 1]})
    tt = np.arange(0, 1700, 1.0)
    axes[0].plot(tt / 60, profile_rate(scenario, tt), color=INK2, lw=1.4)
    axes[0].set_ylabel("carga (req/s)")
    for c in configs:
        ts = sel[c]["ts"]
        axes[1].step(ts.t / 60, ts.pods_ready, where="post", lw=1.6,
                     color=CONFIG_COLOR[c], label=CONFIG_LABEL[c])
    axes[1].set_ylabel("réplicas prontas")
    axes[1].set_ylim(0, MAX_REPLICAS + 0.8)
    axes[1].set_xlabel("tempo desde o início do teste (min)")
    legend(axes[1], loc="upper right")
    for a in axes:
        style(a)
        if xmax:
            a.set_xlim(0, xmax / 60)
    fig.suptitle(title, x=0.01, ha="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def bars(summary, scenario, metric, ylabel, out, title):
    d = summary[summary.scenario == scenario].copy()
    d = d[d[(metric, "mean")].notna()]
    if d.empty:
        return
    d["o"] = d.config.map(CONFIG_ORDER.index)
    d = d.sort_values("o")
    fig, ax = plt.subplots(figsize=(6.5, 3.2))
    x = np.arange(len(d))
    ax.bar(x, d[(metric, "mean")], yerr=d[(metric, "std")].fillna(0), width=0.55,
           color=[CONFIG_COLOR[c] for c in d.config], edgecolor="white", linewidth=2,
           error_kw={"ecolor": INK2, "elinewidth": 1, "capsize": 3})
    for xi, v, sd in zip(x, d[(metric, "mean")], d[(metric, "std")].fillna(0)):
        ax.text(xi, v + sd, f"{v:.0f}", ha="center", va="bottom", fontsize=8, color=INK)
    ax.margins(y=0.12)
    ax.set_xticks(x, [c for c in d.config], fontsize=8)
    ax.set_ylabel(ylabel)
    style(ax)
    fig.suptitle(title, x=0.01, ha="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def stress_curve(runs, out):
    """Latência p95 e vazão atendida em função da carga oferecida (cenário de estresse)."""
    sel = [r for r in runs if r["meta"]["scenario"] == "stress"]
    if not sel:
        return
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.6))
    for r in sel:
        ts, cfg = r["ts"], r["meta"]["config"]
        ramp = ts[(ts.t >= 0) & (ts.t <= 840)]
        off = profile_rate("stress", ramp.t)
        lab = f'{cfg} r{r["meta"]["rep"]}'
        kw = dict(color=CONFIG_COLOR[cfg], lw=1.2, alpha=0.85)
        axes[0].plot(off, ramp.srv_rps_ok, label=lab, **kw)
        axes[1].plot(off, 1000 * ramp.k6_p95, **kw)
    axes[0].plot([0, 420], [0, 420], color=INK2, lw=1, ls="--", label="atendida = oferecida")
    axes[0].set_xlabel("carga oferecida (req/s)")
    axes[0].set_ylabel("vazão atendida (req/s)")
    axes[1].set_xlabel("carga oferecida (req/s)")
    axes[1].set_ylabel("latência p95 (ms)")
    axes[1].set_yscale("log")
    legend(axes[0], loc="upper left")
    for a in axes:
        style(a)
    fig.suptitle("Estresse: capacidade com 8 réplicas", x=0.01, ha="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def formula_fig(fc, out):
    """Taxa de acerto da fórmula ingênua em função da idade (desde o Ready) do pod mais novo."""
    d = fc[fc.resolution_s == 15]
    if d.empty:
        return
    bins = [0, 15, 30, 45, 60, 1e9]
    labels = ["0–15", "15–30", "30–45", "45–60", "> 60"]
    d = d.assign(b=pd.cut(d.youngest_ready_age_s, bins, labels=labels, right=False),
                 ok=(d.predicted == d.desired))
    g = d.groupby("b", observed=False).ok.agg(["mean", "count"])
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    x = np.arange(len(g))
    ax.bar(x, 100 * g["mean"].fillna(0), width=0.55, color=SERIES[0], edgecolor="white", linewidth=2)
    for xi, (m, n) in enumerate(zip(g["mean"], g["count"])):
        if n:
            ax.text(xi, 100 * m, f"{100 * m:.0f}%\n(n={n})", ha="center", va="bottom",
                    fontsize=8, color=INK)
    ax.set_xticks(x, labels, fontsize=8)
    ax.set_xlabel("segundos desde que o pod mais novo ficou Ready")
    ax.set_ylabel("% de decisões = fórmula")
    ax.set_ylim(0, 125)
    style(ax)
    fig.suptitle("Scale-up: quando a fórmula ceil(atual × uso/alvo) vale (resolução 15 s)",
                 x=0.01, ha="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(ROOT / "results"))
    ap.add_argument("--rep", type=int, default=1)
    a = ap.parse_args()
    out = ROOT / "analysis" / "out"
    figs = ROOT / "analysis" / "figs"
    out.mkdir(parents=True, exist_ok=True)
    figs.mkdir(parents=True, exist_ok=True)

    runs = load_runs(Path(a.results))
    if not runs:
        raise SystemExit("nenhuma rodada concluída em " + a.results)
    cost = cpu_per_request(runs)
    print(f"{len(runs)} rodadas; custo de CPU por requisição ≈ {1000 * cost:.2f} ms "
          f"(≈ {0.5 * CPU_REQUEST / cost:.1f} req/s por pod no alvo de 50%)")

    df = pd.DataFrame([run_metrics(r, cost) for r in runs])
    df.to_csv(out / "runs.csv", index=False, float_format="%.3f")

    metrics = [c for c in df.columns if c not in ("id", "scenario", "config", "rep")]
    summary = df.groupby(["scenario", "config"]).agg({"rep": ["count"],
                                                      **{c: ["mean", "std"] for c in metrics}})
    summary = summary.reset_index()
    summary.to_csv(out / "summary.csv", index=False, float_format="%.3f")

    # Markdown: média ± desvio das métricas relevantes por cenário
    cols = {
        "spike": ["detect_s", "first_ready_s", "full_capacity_s", "reaction_p95_max_ms", "slow_s",
                  "fail_pct", "p95_ms", "p99_ms", "scaledown_start_s", "scaledown_min_s",
                  "max_replicas", "overprov_pct"],
        "stress": ["max_served_rps", "offered_at_max_replicas", "offered_at_slow",
                   "offered_at_fail", "fail_pct", "p50_ms", "p95_ms", "restarts"],
        "wave": ["pod_seconds", "ideal_pod_seconds", "overprov_pct", "under_pod_seconds",
                 "scale_events", "fail_pct", "p95_ms", "p99_ms", "max_replicas"],
    }
    lines = [f"Custo de CPU por requisição estimado: {1000 * cost:.2f} ms "
             f"(≈ {0.5 * CPU_REQUEST / cost:.1f} req/s por pod no alvo de 50%).", ""]
    for sc, cs in cols.items():
        d = summary[summary.scenario == sc]
        if d.empty:
            continue
        d = d.assign(o=d.config.map(CONFIG_ORDER.index)).sort_values("o")
        lines += [f"### {sc}", "", "| config | n | " + " | ".join(cs) + " |",
                  "|---|---|" + "---|" * len(cs)]
        for _, row in d.iterrows():
            cells = []
            for c in cs:
                mu, sd = row[(c, "mean")], row[(c, "std")]
                cells.append("—" if pd.isna(mu) else
                             f"{mu:.1f}" + ("" if pd.isna(sd) else f" ± {sd:.1f}"))
            lines.append(f"| {row[('config', '')]} | {row[('rep', 'count')]} | "
                         + " | ".join(cells) + " |")
        lines.append("")
    (out / "summary.md").write_text("\n".join(lines))

    fc = formula_check(runs)
    fc.to_csv(out / "formula.csv", index=False)
    if not fc.empty:
        for kind, g in fc.groupby("kind"):
            ok = (g.predicted == g.desired).mean()
            print(f"fórmula ({kind}): {len(g)} pontos de scale-up, {100 * ok:.0f}% batem exatamente")

    # ---- figuras
    rep = a.rep
    for r in runs:
        mt = r["meta"]
        if mt["config"] == "baseline" and mt["rep"] == rep:
            timeline(r, figs / f"timeline_{mt['scenario']}_baseline.png",
                     f"{mt['scenario']} — baseline (rodada {rep})")
    compare_replicas(runs, "spike", ["baseline", "sync5s", "res60s", "target80"], rep,
                     figs / "spike_configs.png", "Pico: réplicas prontas por configuração",
                     xmax=720)
    compare_replicas(runs, "wave", ["baseline", "stab60", "stab0"], rep,
                     figs / "wave_stabilization.png",
                     "Onda: efeito da janela de estabilização do scale-down", xmax=1620)
    compare_replicas(runs, "wave", ["baseline", "sync5s"], rep,
                     figs / "wave_sync.png", "Onda: sync period 15 s vs 5 s", xmax=1620)
    bars(summary, "spike", "full_capacity_s", "segundos",
         figs / "spike_full_capacity.png", "Pico: tempo até a capacidade final estar pronta")
    bars(summary, "spike", "detect_s", "segundos",
         figs / "spike_detect.png", "Pico: tempo até o HPA reagir (desejadas > 1)")
    bars(summary, "wave", "overprov_pct", "% acima do ideal",
         figs / "wave_overprov.png", "Onda: superprovisionamento (pod·s acima do ideal)")
    stress_curve(runs, figs / "stress_capacity.png")
    formula_fig(fc, figs / "formula.png")
    print(f"tabelas em {out}, figuras em {figs}")


if __name__ == "__main__":
    main()
