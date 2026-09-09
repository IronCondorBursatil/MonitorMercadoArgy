# Portada: distribución estable y uso táctil

Delta solicitado por David el 2026-09-09. Referencia visual y funcional:
`http://129.158.199.209/bonos`. Alcance confirmado: paneles de la portada `/`.
Prioridad agregada: experiencia de celular propia, con funciones de consulta completas
y navegación entre paneles y secciones sobre los mismos componentes.

## Cambio y criterios

- Rechazar movimiento/resize que invada otro panel; permitir contacto de bordes.
  Refrescos SSE, moneda, ley y plazo no modifican la geometría.
- Editar con drag, botones o teclado en escritorio. Aplicar guarda; Cancelar restaura.
  Validar tamaño, límites, ids y colisiones antes de restaurar preferencias.
- En celular, navegar un panel por vez con selector y anterior/siguiente. Vista rápida
  de ticker/precio/tasa principal/MD en las familias compatibles; todas las columnas
  siguen disponibles. Conservar ticker visible durante el scroll de tablas densas.
- Menú compacto conserva los enlaces según permisos. Filtros, detalle, gráfico y
  compartir usan las rutas existentes. Controles frecuentes de 44 CSS px.
- Edición de visibilidad disponible también en celular. El movimiento y resize de la
  grilla se realiza en escritorio; cambiar de breakpoint cancela el borrador y mantiene
  la última geometría aplicada. No existe una segunda grilla ni una copia de los datos.
- Guardar predeterminada para todos sigue siendo una acción admin explícita; no se
  permite si el motor no cargó. Fallback conserva tablas y filtros.

Implementación y ubicación de cada responsabilidad: [flujo-web.md](../../flujo-web.md).
Contrato obligatorio de diseño: [ui-ux.md](../../ui-ux.md).
Auditoría de herramientas: `agents.md §0.4`, sin instalaciones ni cambios de dependencias.

## Plan ejecutado

1. Comparar referencia y código vigente, registrar baseline.
2. Reproducir empuje de vecinos con el motor vendorizado y tests rojos.
3. Adaptar controlador y persistencia, integrar Aplicar/Cancelar y revisar migraciones.
4. Adaptar navegación y lectura táctil sobre el mismo DOM; probar estados y foco.
5. Revisar adversarialmente, probar mutación, gate y smoke, documentar reglas y límites.

## Evidencia reproducible

- Gate `pwsh scripts/check.ps1`: **3244 passed, 8 skipped, 62 warnings**, 272.84 s,
  `GATE VERDE`. Baseline previo: 3243/8/62; los skips conocidos son 3 `tzset` y 5
  que requieren Bash en este harness de Windows. En el hook de Git (con Bash disponible):
  3249 passed, 3 skipped. Evidencia de publicación abajo.
- `node --test tests/js/dashboard_grid.test.cjs`: 10 casos. Se usa el motor real del
  vendor. Al desactivar temporalmente el rechazo de colisiones fallan 3; al restaurarlo
  pasan los 10. Pytest invoca esta suite mediante `tests/test_dashboard_grid.py`.
- `tests/browser/dashboard_fixture.py`: servidor temporal en `127.0.0.1:8001`, stores
  aislados, templates reales, filas y respuestas sintéticas; nunca usa una base operativa.
  Verificar puerto libre antes de arrancarlo, guardar el PID y detener ese proceso al terminar.
- Playwright MCP `browser_run_code_unsafe({filename: <ruta absoluta>})` con
  `tests/browser/dashboard_layout.js`: drag/resize real, botones, teclado, colisiones,
  refresco SSE real, errores de storage, migración cubierta por Node, Apply/Cancel/reload,
  Escape por capas, modal T+0/T+1, navegación móvil y fallback sin vendor.
- `tests/browser/dashboard_touch.js`: contexto Chromium aislado con touch emulado.
  320×740, 390×844, 768×1024 y 844×390, light/dark; tap en controles, menús, ticker fijo,
  gráfico y compartir reales, detalle y entrada de precio. Sin errores JavaScript.
- Inspección visual de portada desktop y celular en ambos temas. Capturas sintéticas en
  `.playwright-mcp/` (directorio de salida permitido por MCP, ignorado por Git).
- Smoke de FastAPI real en :8001 con sandbox de pruebas: `/api/health` 200/degraded
  (loops desactivados), `/login` 200, `/` 302 a `/login`; sin errores de consola en login.

## Límites y cierre compound

No se modificaron dependencias, bases reales, pricing ni el bundle generado de `/on`.
La navegación a otros módulos se conserva; su diseño interno
no se rehízo en esta tarea.

Hardware Android/iOS, Safari, lector de pantalla, teclado virtual, exportación del PNG y zoom real del browser
siguen sin verificar. Las pruebas de ancho no certifican zoom ni conformidad WCAG.
Canvas accesible, sorting completo por teclado y posible upgrade del motor son trabajo
posterior según la auditoría, no prestaciones que esta rama afirme haber resuelto.

Lección materializada: una actualización de datos no cambia la decisión espacial del
usuario; el rechazo de colisiones tiene test guardián con mutación. Las regresiones de
foco y Escape tienen recorridos de browser. Memoria externa: sin cambios; la preferencia
de celular queda versionada en el contrato UI/UX y apuntada desde `CLAUDE.md`.

## Ajuste durante la publicación (2026-09-09)

Primera publicación: `544e823`, CI de main [34345838481](https://github.com/IronCondorBursatil/MonitorMercadoArgy/actions/runs/34345838481)
verde en x86/ARM, `deploy.sh` sin upgrade y freeze sin diferencias. Health de Oracle ok.

La verificación autenticada en Oracle encontró una carrera: una respuesta de filas ya
enviada antes de abrir el detalle podía eliminar el enlace original. `mrRefreshOK` sólo
evita pedidos nuevos. El guard de `htmx:beforeSwap` ahora conserva las filas mientras
un pedido de detalle está pendiente o el modal tiene contenido. El test de navegador
retiene y libera una respuesta para reproducir la desconexión del opener, comprueba
foco al cerrar y exige que los refrescos se reanuden después. Reproducción previa al
fix: enlace desconectado y foco perdido; con el fix: recorrido completo aprobado.
El cierre aborta los pedidos del detalle aún pendientes. La limpieza va en `XHR.loadend`,
independiente del DOM del botón emisor: el test exige que cerrar durante T+0 no congele
las filas ni vuelva a abrir el modal con una respuesta tardía.
