# UI/UX del Monitor — contrato de producto y verificación

Este archivo es la única fuente de verdad de las pautas visuales y de interacción del
Monitor. Las versiones, licencias y veredictos de herramientas viven en
[`agents.md §0.4`](../agents.md#04-herramientas-evaluadas--veredictos-no-volver-a-proponer-las-descartadas);
la arquitectura HTMX SSR y las recetas de paneles viven en
[`docs/flujo-web.md`](flujo-web.md). `CLAUDE.md` sólo apunta acá.

## Lectura obligatoria antes de diseñar o tocar UI

Leer este archivo completo. Después, según el cambio:

- Layout, drag, resize o interacción táctil: [WCAG 2.5.7, Dragging Movements](https://www.w3.org/WAI/WCAG22/Understanding/dragging-movements.html), [WCAG 2.5.8, Target Size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html) y el [changelog de GridStack](https://github.com/gridstack/gridstack.js/blob/master/doc/CHANGES.md) correspondiente a la versión registrada en `agents.md §0.4`.
- Tablas o sorting: [WAI-ARIA APG, Sortable Table](https://www.w3.org/WAI/ARIA/apg/patterns/table/examples/sortable-table/). No copiar el ejemplo sin probarlo: APG advierte que es ilustrativo y que hay diferencias entre combinaciones de browser y tecnología asistiva.
- Gráficos canvas: [accesibilidad de Chart.js](https://www.chartjs.org/docs/latest/general/accessibility.html).
- Foco, responsive o mensajes dinámicos: [Focus Visible](https://www.w3.org/WAI/WCAG22/Understanding/focus-visible), [Reflow](https://www.w3.org/WAI/WCAG22/Understanding/reflow) y [Status Messages](https://www.w3.org/WAI/WCAG22/Understanding/status-messages).
- Una superficie visual nueva: leer la skill oficial [Anthropic frontend-design](https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md) como guía editorial, sin instalarla. Sus preferencias genéricas nunca pisan este contrato ni la identidad existente.

## Contrato del producto

- Conservar FastAPI + Jinja + HTMX SSR + JavaScript y CSS propios. Preferir HTML nativo y
  progresivo antes que otro dueño del DOM.
- `apps/web/static/css/app.css` y los CSS específicos que importe son la fuente de verdad
  ejecutable de colores, tipografía, espacios, estados y temas. Reusar tokens semánticos;
  si falta uno, agregarlo en la fuente existente y cubrir light/dark. No crear otra paleta,
  un archivo de tokens paralelo ni importar una estética genérica.
- Mantener los assets necesarios dentro de `/static/vendor`. No agregar CDN, fuentes
  remotas, telemetría ni un servicio visual externo.
- La densidad es deliberada: es un monitor financiero. Jerarquía, alineación decimal,
  unidades, fecha/plazo/moneda y estados de frescura pesan más que decoración. Color no
  puede ser el único canal de significado.
- Toda función, dato y componente de producto que existe en desktop debe seguir disponible
  en celular. Se pueden reordenar, resumir o revelar progresivamente, pero no retirar ni
  degradar.

## Celular: experiencia táctil propia

Celular no es el dashboard de escritorio encogido. Diseñar primero el recorrido táctil:

- Dar acceso directo a secciones y paneles mediante una navegación operable con una mano,
  con nombre y estado visibles. Preservar la ubicación del usuario al volver de un detalle.
- Presentar un panel principal por tramo de lectura cuando el ancho no permita comparar
  varios. Permitir avanzar, volver, elegir sección y regresar al panel actual sin depender
  de drag ni de precisión fina.
- Mantener acciones frecuentes al alcance, sin tapar datos ni depender sólo de hover.
  Respetar áreas seguras del viewport y los cambios de altura del navegador móvil.
- Los objetivos táctiles deben satisfacer WCAG 2.5.8: al menos 24 × 24 CSS px o separación
  equivalente, salvo una excepción documentada del criterio. Aumentar el área activa sin
  inflar necesariamente el ícono visual.
- Una tabla puede conservar scroll horizontal cuando la relación entre columnas lo exige;
  su título, filtros, selector de sección y acción para volver permanecen accesibles. No
  cortar texto esencial ni ocultar el valor completo detrás de un gesto exclusivo.
- Probar touch emulado, tap, scroll, cambio de orientación y teclado virtual. Hasta ejecutar
  una prueba en hardware real, informar “prueba sintética de navegador”; nunca “probado en
  celular” ni “cumple WCAG”.

## Layout, drag y resize

- GridStack es el único motor del dashboard. SortableJS, interact.js u otro motor paralelo
  requieren un nuevo veredicto explícito en `agents.md §0.4` antes de considerarse.
- Toda operación disponible por drag o resize tiene una alternativa por controles de
  puntero único y por teclado. El handle no puede ser la única forma de mover un panel.
- Editar layout es una transacción: preparar cambios, **Aplicar** para persistir y
  **Cancelar** para restaurar exactamente el estado anterior. Una colisión o posición
  inválida se rechaza sin alterar el layout persistido y se explica en un mensaje de estado.
- Migrar estado legado de forma forward-only: leer y normalizar; no borrar la clave anterior
  al descubrir un valor inválido ni hasta haber persistido y verificado la nueva forma.
- El orden de lectura y tabulación debe seguir un orden lógico predecible después de aplicar.
  Verificarlo expresamente, porque una posición visual distinta del orden DOM desorienta a
  teclado y lector de pantalla.
- Los controles que abren algo mantienen `aria-expanded`; toggles de etiqueta estable usan
  `aria-pressed`; Escape cierra y el foco vuelve al disparador. No anunciar cada tick SSE:
  usar `role="status"` para resultados de acciones, errores y cambios de contexto útiles.

## Tablas, filtros y gráficos

- Conservar `<table>`, `<thead>`, `<th>` y `<tbody>` nativos. No agregar `role="grid"` salvo
  que se implemente y pruebe el modelo compuesto completo de foco y flechas de
  [ARIA APG Grid](https://www.w3.org/WAI/ARIA/apg/patterns/grid/).
- Un encabezado sortable contiene un `<button>` operable por click, Enter y Espacio. El
  `<th>` actualmente ordenado expone `aria-sort="ascending"` o `"descending"`; el ícono es
  redundante y queda fuera del nombre accesible.
- Filtros y contadores actualizados sin mover foco exponen nombre, estado y resultado
  programáticos. Un estado seleccionado debe distinguirse además de por color.
- Todo `<canvas>` tiene nombre accesible y una alternativa equivalente cercana: resumen,
  tabla o enlace a los mismos datos. `role="img"` y un título solos no alcanzan cuando el
  gráfico comunica valores financieros que el usuario necesita consultar.

## Foco, movimiento y responsive

- Todo elemento operable muestra un indicador `:focus-visible` persistente y contrastado
  en light y dark. No quitar el outline sin un reemplazo comprobable.
- Respetar `prefers-reduced-motion`; la animación nunca es necesaria para conocer precio,
  dirección, selección, carga o error.
- A 320 CSS px y a 400 % de zoom no se pierde información ni funcionalidad. Sólo las partes
  que necesitan relación bidimensional —por ejemplo una tabla financiera— pueden conservar
  scroll en dos ejes; navegación y controles deben reflow.
- Los estados loading, vacío, error, stale y dato ausente conservan espacio y semántica
  suficientes para que un swap HTMX no desplace el foco ni haga saltar la tarea activa.

## Evidencia exigida al cerrar un cambio UI

1. Tests de contrato para la lógica nueva; si el bug era semántico, el test debe fallar al
   revertir el atributo, el control o la transición que lo corrige.
2. `/smoke` en `127.0.0.1:8001` y `/verificar-ui` logueado para las rutas tocadas.
3. Recorridos sintéticos de browser en desktop y celular: mouse, teclado, touch emulado,
   panel/sección, detalle, Aplicar/Cancelar, colisión, reload, light/dark y reduced motion
   cuando corresponda. Consola sin errores.
4. Para regresión visual, fijar viewport, browser, SO, tema y datos sintéticos. Los pixels
   cambian entre entornos; un baseline creado en Windows no prueba un runner Linux. Las
   capturas quedan en scratch y no contienen secretos ni bases reales.
5. Reportar por separado: lo comprobado automáticamente, lo inspeccionado en browser, lo
   probado en dispositivo físico y lo no verificado. axe o un screenshot no demuestran
   conformidad WCAG ni equivalencia funcional completa.

## Dónde vive cada hecho

- Reglas visuales, táctiles y de interacción: este archivo.
- Stack, rutas, generación de `on.js` y recetas de paneles: `.claude/rules/web.md` y
  `docs/flujo-web.md`.
- Versiones, licencias y veredictos de herramientas: `agents.md §0.4`.
- Invariantes globales y mapa de lectura: `CLAUDE.md`.
- Conducta ejecutable: tests guardianes y skills `/smoke` y `/verificar-ui`.
