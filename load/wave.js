// Cenário OSCILANTE (onda): subidas e descidas graduais.
// Pergunta: o cluster remove os pods desnecessários nos vales sem prejudicar a
// disponibilidade? Os ciclos curtos (4 min) são menores que a janela padrão de
// estabilização do scale-down (300 s); o ciclo longo (12 min) é maior.
//
// Linha do tempo (TIME_SCALE=1, total ≈ 27 min):
//   0:00–1:00   10 req/s (base)
//   1:00–9:00   2 ciclos curtos: 10 → 70 em 2 min, 70 → 10 em 2 min
//   9:00–21:00  1 ciclo longo:   10 → 70 em 6 min, 70 → 10 em 6 min
//   21:00–27:00 10 req/s (vale final: observa o scale-down completo)
import { buildOptions, hit, stages, rate, summary } from './lib/common.js';

const BASE = 10;
const PEAK = 70;

export const PROFILE = [
  [BASE, 60],
  [PEAK, 120], [BASE, 120],
  [PEAK, 120], [BASE, 120],
  [PEAK, 360], [BASE, 360],
  [BASE, 360],
];

export const options = buildOptions('wave', {
  startRate: rate(BASE),
  stages: stages(PROFILE),
});

export { hit };
export const handleSummary = summary;
