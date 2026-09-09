# Altas ON — evidencia y pendientes

Fuente: `Analisis_52_ON_Argentina_2026-09-08.xlsx`, 52 filas. SHA256:
`328f09b6c3b6e48eeae2c5e20b3a1f64b128c5461db8bff1d2039c2f809eec12`.
El archivo original se leyó sin modificarlo. Manifiesto contractual con fuentes,
capturas, ISIN, especies observadas y flujos explícitos:
[`data/imports/on-2026-09-08.json`](../../../data/imports/on-2026-09-08.json).

## Cobertura

43 ON verificadas: 40 Hard Dollar y 3 Dollar Linked. Un registro por obligación;
sus variantes ARS/MEP/CCL comparten ISIN. Sólo se cargan símbolos observados.

| Sector | ON |
|---|---:|
| Energía / Petróleo & Gas | 14 |
| Servicios Financieros | 14 |
| Agro / Alimentos | 7 |
| Utilities (Luz / Gas) | 3 |
| Infraestructura / Construcción | 2 |
| Real Estate | 2 |
| Salud / Farma | 1 |

Pendientes: **MR43O, MR44O, MR45O, MR47O y MR50O**. Los contratos permiten
elegir entre efectivo y capitalización de intereses (PIK); un calendario de efectivo
único supondría una decisión del emisor. Requieren soporte de escenarios o una elección
confirmada. Las cuatro especies ARS/TAMAR **DEC3O, LR8AO, STCHO y SXC6O** no integran
el alcance HD/DL. No quedan términos faltantes entre las 43 del manifiesto.

## Controles financieros

Unidades: USD por 100 VN **originales**, también en las restructuraciones.
Rizo VIII conserva VR 60 y amortización final 22,5; Rizo XI conserva VR 80 tras el pago
inicial 20. Se verificaron cupones largos de PLC7O, ZPC5O y TBCAO; primer cupón irregular
de MSU; emisión original de la reapertura DN1AO; y amortizaciones no bullet.

La fuente contractual definitiva prevalece sobre discrepancias de BYMA: vencimientos
de PFC4O/YFCPO y cupón 9% de RZBAO. MUC4O ACT/365 corroborado en suplemento A3. Ley,
moneda de pago y sector se guardan por separado; USD cable no implica ley extranjera.

Los calendarios usan fechas programadas, conforme a las convenciones del Monitor.
Excepción contractual explícita HT2MD: vencimiento legal 30/06/2029, pago final 29/06/2029
y último interés por 60 días sobre VR 9,10. El calendario se revisará si aparecen nuevos
feriados. Opciones futuras de rescate no se convierten en pagos ciertos.

El control de VT detectó y corrigió dos errores de corridos ACT: capital previo a una
amortización e historia truncada interpretada como un período de varios años.
También reveló el primer cupón de 9 meses de ZPC5O: la fecha explícita de inicio de
devengamiento evita que la frecuencia trimestral produzca un inicio inferido futuro y
corridos en cero. El dato viaja por ambas lecturas de catálogo y por la clave del memo;
la emisión legal se conserva.
Convención y alcance: [`docs/convenciones-financieras.md`](../../convenciones-financieras.md).

## Ejecución y comprobaciones

**Aplicadas a la base local:** 43 altas, 40 HD y 3 DL. Catálogo final: **792 registros y 3612 flujos**; la relectura confirmó idénticos los 749 instrumentos y 3330 flujos
anteriores. Son 78 especies de moneda nuevas. Las ON del catálogo totalizan 240:
209 HD y 31 DL. Una segunda ejecución dry-run reconoció las 43 y propuso cero altas.

Respaldo previo, conservando todos los backups anteriores:
[catalog-2026-09-08T222956-pre-on-20260908.db](C:/Users/david/AppData/Local/monitor/backups/catalog-2026-09-08T222956-pre-on-20260908.db). No se usó force ni se resembró la base.

- **Gate final:** Ruff verde, **3243 passed, 8 skipped, 62 warnings**, 257,24 segundos.
- **Pricing independiente posterior al alta:** 43/43, incluyendo VT/corridos al 8 y
  9 de septiembre. 39 cotizaciones observadas cubren 32 ON; las restantes 11 se
  verificaron con precios de control. Error máximo de NPV independiente 4,68e-11 USD;
  recálculo de precio 6,72e-8. La base de entrada no se alteró durante la verificación.
- **App real aislada:** health/login 200, protección sin cookie 302, login real y
  `/on/data` correctos; 63 especies nuevas con precio presentes, 38 controles numéricos
  de patas USD. ZPC5D VT al 08/09 = 101,317808219; Rizo XI = 81,400547945. Puerto 8001 libre.
  Las especies sin precio permanecen en el catálogo; la pantalla requiere cotización.
- **Review:** 204 capturas fuente con SHA comprobado, sectores 43/43; importador,
  pricing, lecturas y guardián TX28 revisados independientemente.

El primer gate detectó dos contrastes negativos de TX28 que dejaron de separar ACT
por paridad tras corregir corridos. David autorizó su ajuste: se conservaron los
**goldens externos y sus tolerancias**, y el day-count se controla contra la fórmula
contractual `50 × 0,0225 × 115 / 360 = 0,359375`. ACT da `0,36049723756906077`, una
diferencia de `0,00112223756906077`. La mutación forzando ACT produjo dos fallos y fue
restaurada. Los controles de settlement/CER externos permanecen intactos.

Evidencia detallada en el scratch: `live-apply.json`, `live-repeat.json`,
`live-pricing-43.json`, `smoke-app-43-fixed.json`, `gate-final.txt` y
`completion-summary.json`. Este informe registra el cierre de la carga local en
**feat/altas-on-20260908**. David autorizó después integrar y publicar código y datos.
El resultado operativo remoto (SHA, CI, respaldo, conteos y salud) se conserva en
el subdirectorio `produccion/` del mismo scratch y en el reporte de la sesión.

La TIR/MD y el recálculo del precio usan liquidación 09/09/2026 (T+1 de 08/09).
El VT se controla independientemente para ambas fechas; la API de VT de HD/DL recibe
la fecha de referencia directamente. Los precios de control y las cotizaciones reales
se informan separados. Una cotización con TIR calculada internamente **no es un golden
externo de TIR**. Las capturas de Data912 y FX tienen horarios distintos y no incluyen
hora de última operación por especie.

Los reportes detallados y capturas viven en
`.superpowers/sdd/2026-09-08-altas-on/`. Las filas anteriores deben conservarse idénticas;
la segunda ejecución debe reconocer las altas sin escribir. El importador hace respaldo
obligatorio y usa `create_only` transaccional: una coincidencia ajena aborta, nunca hace
upsert. Cada instrumento tiene su transacción; el lote no es atómico y una recuperación
desde el respaldo completo debe ser manual, revisando antes operaciones posteriores.

## Compound

- Invariantes materializados: sólo altas con respaldo, colisiones concurrentes, especies
  en su slot correcto, flujos sin campos extra y fecha final contractual explícita:
  `tests/test_import_on_manifest.py`.
- Guardianes financieros: `tests/test_on_import_20260908_data.py` y
  `tests/test_accrued_amortization.py`. Mutaciones verificadas: retirar `create_only`
  y reemplazar el primer cupón largo de PLC7O por uno regular produce rojo.
- Documentación corregida: sección de corridos en convenciones financieras; spec y plan
  de esta tarea explican el ajuste que bloqueó la validación.
- Memoria de sesión: `project_altas_on_2026_09_08.md`, con puntero a este informe.
- Fuera de alcance: cinco ON con PIK y cuatro ARS/TAMAR.
  El trabajo está aislado en `feat/altas-on-20260908`; los worktrees previos de Manager
  y TAMAR permanecen separados.
