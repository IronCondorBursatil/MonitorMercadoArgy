# Plan de ejecución — altas ON

1. Leer la planilla sin modificarla, guardar hash y confrontar las tres especies e ISIN
   con el catálogo local mediante SQLite read-only.
2. Verificar términos contractuales en tres investigaciones independientes: financieras,
   corporativas y canjes. Separar ready, fuera de alcance y pendientes documentados.
3. Probar primero los guards del importador (colisiones, datos inválidos, respaldo,
   preservación e idempotencia); implementar el mínimo borde sobre `save_instrument`.
4. Materializar manifiesto con fuentes, sector canónico y calendario explícito. Ensayar
   la carga sobre snapshot externo; probar el pricing con settlement fijo y FX separados.
   Incluir controles independientes de corridos/VT: amortización reciente (Rizo XI),
   historial recortado (Rizo VIII) y pago final anterior al vencimiento (HT2MD).
5. Ejecutar gate (`pwsh scripts/check.ps1`) y review independiente. Corregir únicamente
   problemas del alcance. Aplicar altas locales con respaldo y comprobar anteriores
   intactos, recarga y segunda ejecución sin escrituras.
6. Entregar conteos, sectores, pendientes precisos, evidencia de tests y respaldo.
   Compound: guard de sólo-altas materializado en tests; procedencia y limitaciones
   conservadas en manifiesto/reporte.
7. Ampliación autorizada: integrar y publicar esta rama; exigir CI x86/ARM verde para
   el SHA de main, desplegar con `deploy.sh` sin upgrade. Comparar ticker/ISIN en la
   base remota antes de la carga; detener brevemente el servicio, aplicar el manifiesto
   con backup obligatorio y volver a iniciarlo también si el importador falla.
8. Releer las 43 altas y los registros anteriores, comprobar idempotencia, salud remota
   y visualización autenticada de `/on`. Guardar evidencia en el scratch `produccion/`.
