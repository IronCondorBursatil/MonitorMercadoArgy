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
- **Motor legacy**: `tests/_legacy_engine.py` comparte a propósito el solver, `pricing.metrics`
  y `days_30_360` con producción. **No** copiárselos. (El test tautológico de 30/360 quedó
  cubierto en la Fase 5 con 12 valores a mano: `test_days_30_360_valores_a_mano`.)
- **Goldens externos** (agents.md §0.6). Los 13 ONs + 2 dólar-linked + 2 LECAP son contra la
  calculadora del broker del autor (anonimizada a propósito en a0c2e5f; procedencia en el
  docstring de `tests/test_golden_referencia.py`) — un tercero independiente (IAMC/BYMA) sigue
  faltando para esos. **CER**: corte externo conseguido para TX28 (Banco Hipotecario, Informe
  Diario, cierre 24hs BYMA del 2026-09-03: precio 1.719, TIR 8,82 %, paridad 93,14 %, MD 1,08)
  → golden en `tests/test_golden_tx28.py` (verde o `xfail` documentado, según cuadre).
  **TAMAR**: TTJ26 venció el 2026-06-30 (la validación IAMC del docstring de
  `core/domain/pricing/tamar.py` es de junio y no se puede reproducir sin la serie de ese
  día); sustitutos vivos con corte del mismo informe: TTS26 (169,10 / TIR 22,56 %) y TTD26
  (169,00 / 24,78 %). IAMC no publica el informe diario desde 2026-05-26 y su feed en BYMA
  open está paywalleado.
- **TMVE8: verificar la primera cotización cargada** contra `V.Téc = max(tc_inicial × riel
  TAMAR, 100 × A3500)` — la escala se confirmó el 2026-09-08 con Data912 (`c=139680`, o sea
  pesos por 100 VN denominados en USD, como TZVD8/D31M7 y no como los duales TAMAR), pero
  todavía no hubo una rueda con el papel en el catálogo: el primer día que aparezca, contrastar
  precio, V.Téc y paridad antes de dar por buena la columna.
- **`/escenarios`: `DUAL_DL_TAMAR` hereda beta FX 0 del grupo TAMAR** — cuando manda el riel
  dólar la posición se mueve ~1:1 con el dólar, así que el escenario la muestra insensible al
  FX y subestima el impacto. Revisar cuando haya posición cargada.
- **`scripts/init_admin.py` usa `Base.metadata.create_all`** y no `init_db()` de
  `catalog_repository` (hallazgo lateral de la prueba de docs 2026-09-07, ya señalado en
  `docs/auditoria-2026-08-31.md` ítem 2.3, **no verificado en vivo**): sobre una DB restaurada
  de un backup viejo, `create_all` no corre la migración de columnas y el login puede reventar
  con `no such column: users.allowed_tabs`.
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
