# Pendiente (cola, no funcional)

Movido desde `CLAUDE.md` en la Fase 4 (agents.md §0.8). Es la cola de cosas conocidas que
no bloquean: no es un plan (el plan por fases vive en `agents.md › §0.8`) ni un registro
de decisiones (`docs/decisiones.md`).

- **Providers sync restantes**: el hot-path (fuente live + floor, cada 5s) corre async por
  `ResilientClient` + `ProviderHub`, y FX/indices/REM ya tienen `async def prefetch(client)`
  cableado en el `_refresh_loop`. Quedan 100% sync **CAFCI** y **argentinadatos**: `httpx.get`
  directo con cache propio por TTL, corriendo dentro del cómputo de pricing (o sea en
  `to_thread`, fuera del event loop). Es **deliberado** — pasarlos a `ResilientClient` es bajo
  valor / alto riesgo. Ojo: en el camino sync **no hay retry** (el único vive en
  `ResilientClient`) — un provider NUEVO va async, no acá. El helper viejo
  `core/infrastructure/_http.py::http_get_json` **ya no existe** (borrado en f442452); si un doc
  o un comentario todavía lo nombra, está viejo.
- **`optionlab` arrastra `jupyter<2` a prod** (agents.md §0.3): `optionlab` 1.8.5 lo declara
  como dependencia dura, así que el servidor instala ~60 paquetes del ecosistema Jupyter sin
  usarlos, y un freeze completo los pinea. También capa `pandas<3` y `holidays<0.45`. Salida:
  `--no-deps` o vendorizar la parte que se usa (payoff multi-leg). Fuera de alcance por ahora.
- **`tests/test_daycount.py:107-120` es tautológico** (agents.md §0.6): compara
  `DayCount.THIRTY_360` contra `days_30_360`, que es la función que lo implementa. Falta
  agregar valores 30/360 calculados a mano para los casos 31→30 y 29-feb (Fase 5). **No**
  copiar los símbolos vivos al motor legacy: `_legacy_engine.py` los comparte a propósito.
- **Goldens externos: 0 para CER, 0 para TAMAR** (agents.md §0.6). Hay 13 ONs hard-dollar +
  2 dólar-linked + 2 LECAP contra "la calculadora de referencia", sin procedencia registrada.
  La validación IAMC de TTJ26 vive solo en el docstring de `core/domain/pricing/tamar.py`.
  Candidato BONCER: TX28 (no TX26, que vence en 2026 con un solo flujo); falta el corte
  externo (fecha + precio + CER del día + TIR/paridad publicada). Fase 5.
- **Backfill del ancla TAMAR en producción**: `scripts/backfill_tamar_anchor.py` ya corrió en
  la `catalog.db` local (14 bonos, 2026-09-04); en el servidor exige servicio parado y
  `MONITOR_DB_DIR` explícito (`deploy/README-ops.md › Scripts manuales`). Ojo con qué compra:
  `/cashflows` y el pricing son IGUALES con o sin ancla (la fila del panel se sintetiza desde
  `maturity_date`); lo único que cambia es la tabla de completitud del ABM, donde `cfn` es un
  `COUNT(*)` crudo y esos bonos figuran en rojo por «falta Cashflows» hasta que se corra.
- Charts/sparklines adicionales (Chart.js) — ya usado en el panel FCI (`static/js/fci.js`);
  extender a otros paneles. Más cobertura de tests de routers.
- **FCI composición de cartera**: única pieza no disponible (CAFCI ficha gateada / worker de
  fonditos pago). El panel la omite hasta conseguir fuente. Flujos: reales vía `fci_history`
  a medida que acumula ruedas; lente 3m/6m/12m se completa cuando el bootstrap de ~400d de
  CER/A3500 backfillee.
- **Backup fuera de la caja**: el bundle de `scripts/backup_bundle.py` queda en el mismo disco
  del servidor; falta el destino en Object Storage y la copia periódica a la laptop
  (`deploy/README-ops.md › Backups`).
- **Comentarios de código que todavía nombran la VM anterior** (`apps/web/app.py`,
  `config/settings.py`, `deploy.sh`, `core/infrastructure/_tls.py`): se leen "servidor
  Oracle" (agents.md §0.2). No se corrigen en la Fase 4 porque son código, no docs; limpiar
  cuando se toque cada archivo.
