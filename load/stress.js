// Cenário de ESTRESSE: rampa contínua até bem além da capacidade com maxReplicas.
// Pergunta: como a aplicação se comporta quando o HPA atinge maxReplicas e a
// demanda continua subindo?
//
// Linha do tempo (TIME_SCALE=1, total ≈ 16 min):
//   0:00–12:00  5 → 420 req/s  (~35 req/s por minuto)
//               ~85 req/s  (≈ 2:20): HPA deve chegar a maxReplicas=8
//               ~340 req/s (≈ 9:40): pods no limite de CPU → saturação
//   12:00–14:00 420 req/s      (sobrecarga sustentada)
//   14:00–14:30 420 → 5 req/s
//   14:30–16:00 5 req/s        (recuperação)
import { buildOptions, hit, stages, rate, summary } from './lib/common.js';

export const PROFILE = [
  [420, 720],
  [420, 120],
  [5, 30],
  [5, 90],
];

export const options = buildOptions('stress', {
  startRate: rate(5),
  stages: stages(PROFILE),
});

export { hit };
export const handleSummary = summary;
