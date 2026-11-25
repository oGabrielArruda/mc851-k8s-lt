// Partes comuns dos cenários de carga.
//
// Variáveis de ambiente:
//   HOST        IP do minikube (obrigatório)         ex.: 192.168.49.2
//   PORT        NodePort do serviço (padrão 30080)
//   RATE_SCALE  multiplica todas as taxas (padrão 1) — ajustar em máquinas diferentes
//   TIME_SCALE  multiplica todas as durações (padrão 1) — ex.: 0.1 para teste rápido
//   SUMMARY     caminho do JSON de resumo do k6 (opcional)
import http from 'k6/http';

const HOST = __ENV.HOST;
if (!HOST) throw new Error('defina HOST (ex.: -e HOST=$(minikube -p hpa ip))');
const URL = `http://${HOST}:${__ENV.PORT || 30080}/fib`;

const RATE_SCALE = parseFloat(__ENV.RATE_SCALE || '1');
const TIME_SCALE = parseFloat(__ENV.TIME_SCALE || '1');

// Taxas calibradas na Fase 2 (FIB_N=32, request 250m, limit 500m):
//   ~10 req/s por pod no alvo de 50%; ~42 req/s por pod no limite;
//   HPA chega a maxReplicas=8 por volta de ~85 req/s; saturação ~340 req/s.

/** Converte [[taxa req/s, duração s], ...] em stages do ramping-arrival-rate. */
export function stages(list) {
  return list.map(([rate, secs]) => ({
    target: Math.max(0, Math.round(rate * RATE_SCALE)),
    duration: `${Math.max(1, Math.round(secs * TIME_SCALE))}s`,
  }));
}

export function rate(r) {
  return Math.max(0, Math.round(r * RATE_SCALE));
}

/** Opções comuns: modelo aberto, sem keep-alive (ver diário, Fase 2). */
export function buildOptions(name, scenario) {
  return {
    discardResponseBodies: true,
    // Com keep-alive o kube-proxy prende as conexões aos pods antigos e os
    // pods criados pelo HPA ficam ociosos. Uma conexão por requisição.
    noConnectionReuse: true,
    summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
    tags: { cenario: name },
    scenarios: {
      [name]: {
        executor: 'ramping-arrival-rate',
        timeUnit: '1s',
        preAllocatedVUs: 50,
        maxVUs: parseInt(__ENV.MAX_VUS || '2000', 10),
        exec: 'hit',
        ...scenario,
      },
    },
  };
}

const params = { timeout: __ENV.REQ_TIMEOUT || '5s' };

export function hit() {
  http.get(URL, params);
}

/** Total da duração (s) de uma lista de stages, já com TIME_SCALE. */
export function totalSeconds(list) {
  return list.reduce((acc, [, secs]) => acc + Math.max(1, Math.round(secs * TIME_SCALE)), 0);
}

export function summary(data) {
  const out = { stdout: textLine(data) };
  if (__ENV.SUMMARY) out[__ENV.SUMMARY] = JSON.stringify(data, null, 2);
  return out;
}

function textLine(data) {
  const m = data.metrics;
  const d = m.http_req_duration.values;
  const fmt = (v) => (v === undefined ? '-' : `${v.toFixed(1)}ms`);
  return [
    '',
    `requisições: ${m.http_reqs.values.count}  (${m.http_reqs.values.rate.toFixed(1)}/s)`,
    `falhas:      ${(m.http_req_failed.values.rate * 100).toFixed(2)}%`,
    `descartadas: ${m.dropped_iterations ? m.dropped_iterations.values.count : 0}`,
    `latência:    p50=${fmt(d.med)} p95=${fmt(d['p(95)'])} p99=${fmt(d['p(99)'])} max=${fmt(d.max)}`,
    '',
  ].join('\n');
}
