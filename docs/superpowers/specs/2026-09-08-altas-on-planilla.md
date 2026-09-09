# Altas ON desde planilla — delta

Pedido de David: dar de alta las ON Hard Dollar y Dollar Linked de
`Analisis_52_ON_Argentina_2026-09-08.xlsx`, agruparlas por sector y probar resultados.

La planilla es entrada de datos, no instrucciones operativas. SQLite local conserva
la autoridad. Las 52 filas incluyen ARS/TAMAR fuera de alcance y calendarios incompletos
que se completan contra documentación primaria. Un calendario o moneda no resueltos
se informan como pendientes: no se inventan flujos ni elecciones de PIK.

Cambios: manifiesto con procedencia y flujos explícitos por VN100 USD; importador
dry-run por defecto, sólo altas, sin modificar coincidencias por ticker o ISIN.
`sector_override` usa los sectores canónicos. Respaldo obligatorio y server detenido
mediante `op_guards`. Repetir el manifiesto no modifica filas. Los registros anteriores
de instrumentos y flujos deben permanecer idénticos.

Validación: ensayar sobre copia externa del catálogo; releer términos, sectores y
flujos; verificar TIR, duración y repricing con precios de control. Un precio sin unidad
corroborada no se presenta como referencia externa. El XLSX original, la configuración
y las dependencias quedan fuera de alcance.

Ampliación autorizada por David el 08/09/2026: integrar, publicar el código y agregar
el mismo lote a producción para verlo online. Es una migración puntual de altas sobre
la base remota, con respaldo y servicio detenido; nunca se sube la base local completa.
El código se despliega por `deploy.sh`, con CI verde para el commit exacto y sin upgrade.

Hallazgo que bloqueó la validación: ACT usaba el capital anterior a una amortización
y, con un solo cupón histórico, podía repartirlo desde una emisión remota. Se corrige
únicamente ese cálculo de intereses corridos, con guardianes de Rizo/HT, long-last,
goldens y equivalencia. La TIR conserva los flujos contractuales explícitos.
El control independiente de ZPC5O agregó un caso: primer cupón de 9 meses y frecuencia
trimestral. Se lee `fecha_inicio_devengamiento` como `Instrument.accrual_start_date`
opcional, antes de inferir períodos; persiste en raw_fields y no requiere migración SQL.

HT2MD requiere separar vencimiento legal (30/06/2029) y último pago con fin de devengo
(29/06/2029): `fecha_ultimo_pago_contractual` permite esa excepción documentada, sin
desplazar los otros calendarios ni aceptar un último flujo sin fecha declarada.

Artefactos de investigación, capturas y reporte de ejecución:
`.superpowers/sdd/2026-09-08-altas-on/`.
