// Cenário de PICO: carga baixa estável → salto brusco → sustenta → volta ao baixo.
// Pergunta: quanto tempo o HPA leva para disponibilizar pods e quantas
// requisições falham nesse intervalo?
//
// Linha do tempo (TIME_SCALE=1, total ≈ 12 min):
//   0:00–1:00  5 req/s        (1 pod, ~50% do alvo)
//   1:00–1:10  5 → 60 req/s   (salto: precisa de ~6 pods; 1 pod suporta ~42)
//   1:10–5:10  60 req/s       (sustentado)
//   5:10–5:20  60 → 5 req/s
//   5:20–12:00 5 req/s        (observa o scale-down, janela padrão de 300 s)
import { buildOptions, hit, stages, rate, summary } from './lib/common.js';

export const PROFILE = [
  [5, 60],
  [60, 10],
  [60, 240],
  [5, 10],
  [5, 400],
];

export const options = buildOptions('spike', {
  startRate: rate(5),
  stages: stages(PROFILE),
});

export { hit };
export const handleSummary = summary;
