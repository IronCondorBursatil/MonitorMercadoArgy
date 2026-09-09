// Reproducible con Playwright MCP browser_run_code_unsafe({filename: <este archivo>}).
// Requiere dashboard_fixture.py en :8001. Rechaza producción y restaura el storage.
async (page) => {
  function check(value, message) { if (!value) throw new Error(message); }
  function equal(a, b, message) { check(JSON.stringify(a) === JSON.stringify(b), message); }
  check(page.url().startsWith('http://127.0.0.1:8001/'), 'Sólo fixture local');
  const health = await (await page.request.get('http://127.0.0.1:8001/api/health')).json();
  check(health.synthetic === true, 'Se requieren datos sintéticos');
  const keys = ['monitor-dashboard-state-v1', 'grid-layout-v3', 'panels-hidden-v1', 'theme'];
  const stored = await page.evaluate(keys => keys.map(key => [key, localStorage.getItem(key)]), keys);
  const viewport = page.viewportSize();
  const errors = [];
  const onError = error => errors.push(String(error));
  page.on('pageerror', onError);
  const result = [];
  const geometry = () => page.evaluate(() => Array.from(document.querySelectorAll('.grid-stack-item')).map(e => ({
    id: e.getAttribute('gs-id'), x: Number(e.getAttribute('gs-x')), y: Number(e.getAttribute('gs-y')),
    w: Number(e.getAttribute('gs-w')), h: Number(e.getAttribute('gs-h')), hidden: e.hidden,
  })).sort((a, b) => a.id.localeCompare(b.id)));
  const begin = async () => {
    await page.locator('#nav-config-btn').click();
    await page.locator('[data-action="edit"]').click();
    check(await page.locator('#layout-edit-bar').isVisible(), 'No entró al modo edición');
  };
  const controls = async () => {
    if (!await page.locator('.layout-controls').getAttribute('open')) {
      // El atributo open vacío también representa abierto: isVisible mide el select.
      if (!await page.locator('#layout-panel').isVisible()) await page.locator('.layout-controls summary').click();
    }
  };
  const emptyStorage = () => page.evaluate(keys => keys.forEach(key => localStorage.removeItem(key)), keys.slice(0, 3));
  try {
    await page.setViewportSize({width: 1440, height: 1000});
    await emptyStorage(); await page.reload();
    await page.locator('.dashboard-ready').waitFor();
    check(!await page.locator('.dashboard-fallback').count(), 'Falló el controlador');
    const original = await geometry();
    const first = page.locator('[gs-id="bonares"]');
    await begin();
    const handle = first.locator('.layout-drag-handle');
    let box = await handle.boundingBox();
    await page.mouse.move(box.x + 12, box.y + 12); await page.mouse.down();
    await page.mouse.move(box.x + 200, box.y + 12, {steps: 12}); await page.mouse.up();
    equal(await geometry(), original, 'El drag empujó a un vecino');
    box = await first.locator('.ui-resizable-se').boundingBox();
    await page.mouse.move(box.x + 12, box.y + 12); await page.mouse.down();
    await page.mouse.move(box.x + 112, box.y + 40, {steps: 12}); await page.mouse.up();
    equal(await geometry(), original, 'El resize empujó a un vecino');
    result.push('drag y resize reales rechazan colisión');

    await controls();
    await page.locator('[data-layout-step="narrow"]').click();
    await handle.focus(); await page.keyboard.press('ArrowRight');
    check(await first.getAttribute('gs-w') === '90' && await first.getAttribute('gs-x') === '10', 'Click/teclado no mueve a espacio libre');
    const draft = await geometry();
    await page.locator('#nav-config-btn').click(); await page.keyboard.press('Escape');
    equal(await geometry(), draft, 'Escape del menú canceló el borrador');
    check(await page.locator('#layout-edit-bar').isVisible(), 'Escape del menú salió de edición');
    const response = page.waitForResponse(r => r.url().includes('/panels/bonares/rows'), {timeout: 12000});
    await response;
    equal(await geometry(), draft, 'El refresco SSE/HTMX cambió geometría');
    check(await page.evaluate(() => localStorage.getItem('monitor-dashboard-state-v1')) === null, 'Se guardó sin aplicar');
    await page.locator('#layout-cancel').click();
    equal(await geometry(), original, 'Cancelar no restauró geometría');
    result.push('click, teclado, Escape por capas, SSE y Cancelar');

    await begin();
    await first.locator('[data-act="close"]').click();
    check(!await first.isVisible(), 'Ocultar no funcionó');
    await page.locator('#layout-cancel').click();
    equal(await geometry(), original, 'Cancelar no restauró panel oculto');
    await begin(); await controls();
    await page.locator('[data-layout-step="narrow"]').click();
    await page.evaluate(() => {
      window.__originalSetItem = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key, value) {
        if (key === 'monitor-dashboard-state-v1') throw new Error('Cuota de prueba');
        return window.__originalSetItem.call(this, key, value);
      };
    });
    await page.locator('#layout-apply').click();
    check(await page.locator('#layout-edit-bar').isVisible(), 'Un error de storage perdió el borrador');
    check(await page.locator('#layout-edit-status.is-error').count() === 1, 'No informó error al guardar');
    await page.evaluate(() => { Storage.prototype.setItem = window.__originalSetItem; });
    await page.locator('#layout-apply').click();
    const applied = await geometry();
    await page.reload();
    equal(await geometry(), applied, 'Aplicar no sobrevivió a la recarga');
    result.push('ocultar/cancelar, fallo de storage y aplicar/recargar');

    const saved = await page.evaluate(() => localStorage.getItem('monitor-dashboard-state-v1'));
    for (const width of [1920, 1280, 768, 390, 320]) {
      await page.setViewportSize({width, height: 900});
      const dimensions = await page.evaluate(() => ({client: document.documentElement.clientWidth,
        scroll: document.documentElement.scrollWidth,
        panels: [...document.querySelectorAll('.grid-stack-item:not([hidden])')].map(e => {
          const r = e.getBoundingClientRect(); return {left:r.left, right:r.right, top:r.top, bottom:r.bottom};
        })}));
      check(dimensions.scroll <= dimensions.client, 'Desborde horizontal global a ' + width);
      for (let i = 0; i < dimensions.panels.length; i++) for (let j = i + 1; j < dimensions.panels.length; j++) {
        const a = dimensions.panels[i], b = dimensions.panels[j];
        check(a.right <= b.left + 1 || b.right <= a.left + 1 || a.bottom <= b.top + 1 || b.bottom <= a.top + 1,
          'Paneles superpuestos a ' + width);
      }
      equal(await page.evaluate(() => localStorage.getItem('monitor-dashboard-state-v1')), saved, 'Viewport sobrescribió el layout');
    }
    result.push('1920/1280/768/390/320 px sin desborde ni superposición');
    await page.setViewportSize({width:1440, height:1000});
    await begin(); await controls(); await page.locator('[data-layout-step="narrow"]').click();
    await page.setViewportSize({width:390, height:900});
    await page.waitForFunction(() => document.getElementById('layout-edit-bar').hidden);
    check(!await page.locator('#layout-edit-bar').isVisible(), 'El paso a móvil no canceló la edición');
    await page.setViewportSize({width:1440,height:1000});
    equal(await geometry(), applied, 'El paso a móvil destruyó el estado aplicado');

    await first.locator('[data-act="cfg"]').click();
    check(await first.locator('[data-act="cfg"]').getAttribute('aria-expanded') === 'true', 'Falta aria-expanded');
    await page.keyboard.press('Escape');
    check(await first.locator('[data-act="cfg"]').getAttribute('aria-expanded') === 'false', 'aria-expanded quedó desactualizado');
    await first.locator('.ccy-btn[data-ccy="ARS"]').click();
    check(await first.locator('.ccy-btn[data-ccy="ARS"]').getAttribute('aria-pressed') === 'true', 'Falta aria-pressed');
    await first.locator('.ccy-btn[data-ccy="MEP"]').click();
    const link = first.locator('tbody a').first();
    await link.focus(); await link.click();
    await page.locator('#modal .modal-card').waitFor();
    await page.locator('#modal button', {hasText:'T+0'}).focus();
    await page.keyboard.press('Enter');
    await page.locator('#modal .toggle button.on', {hasText:'T+0'}).waitFor();
    check(await page.locator('#modal').evaluate(e => e.contains(document.activeElement)), 'Swap T+0 perdió foco');
    await page.keyboard.press('Tab');
    check(await page.locator('#modal').evaluate(e => e.contains(document.activeElement)), 'Tab escapó del detalle');
    await page.keyboard.press('Escape');
    check(await page.locator('#modal').evaluate(e => e.children.length === 0), 'Escape no cerró modal');
    check(await link.evaluate(e => e === document.activeElement), 'El modal no devolvió el foco');
    result.push('estados accesibles y modal con foco restaurado');

    // Un refresco ya enviado puede responder mientras se consulta el detalle.
    let releaseRows;
    let rowsStarted;
    let rowsFulfilled;
    const holdRows = new Promise(resolve => { releaseRows = resolve; });
    const requestedRows = new Promise(resolve => { rowsStarted = resolve; });
    const fulfilledRows = new Promise(resolve => { rowsFulfilled = resolve; });
    const delayedRows = async route => {
      const response = await route.fetch();
      rowsStarted(); await holdRows;
      await route.fulfill({response}); rowsFulfilled();
    };
    await page.route('**/panels/bonares/rows*', delayedRows);
    try {
      await page.evaluate(() => htmx.trigger(document.getElementById('tbody-bonares'), 'tabvisible'));
      await requestedRows;
      await link.focus(); await link.click();
      await page.locator('#modal .modal-card').waitFor();
      const opener = await link.elementHandle();
      await page.evaluate(() => {
        window.__rowsRequestEnded = false;
        document.body.addEventListener('htmx:afterRequest', function ended(e) {
          if (e.detail.elt.id !== 'tbody-bonares') return;
          window.__rowsRequestEnded = true;
          document.body.removeEventListener('htmx:afterRequest', ended);
        });
      });
      releaseRows(); await fulfilledRows;
      await page.waitForFunction(() => window.__rowsRequestEnded);
      check(await opener.evaluate(e => e.isConnected), 'Un refresco en vuelo quitó el enlace del modal');
      await page.keyboard.press('Escape');
      check(await opener.evaluate(e => e === document.activeElement), 'La carrera de SSE perdió el foco');
    } finally {
      releaseRows(); await page.unroute('**/panels/bonares/rows*', delayedRows);
      if (await page.locator('#modal .modal-card').count()) await page.keyboard.press('Escape');
    }
    const previousRow = await link.elementHandle();
    await page.evaluate(() => htmx.trigger(document.getElementById('tbody-bonares'), 'tabvisible'));
    await page.waitForFunction(e => !e.isConnected, previousRow);
    result.push('respuesta SSE en vuelo conserva el enlace y el foco del detalle');

    // Cerrar durante T+0 elimina el emisor: limpiar por loadend, no por bubbling DOM.
    let releaseDetail;
    let detailStarted;
    const holdDetail = new Promise(resolve => { releaseDetail = resolve; });
    const requestedDetail = new Promise(resolve => { detailStarted = resolve; });
    const delayedDetail = async route => {
      const response = await route.fetch(); detailStarted();
      await holdDetail; await route.fulfill({response});
    };
    await link.click(); await page.locator('#modal .modal-card').waitFor();
    await page.route('**/bond/*/detail?lag=0', delayedDetail);
    try {
      await page.evaluate(() => {
        window.__detailEnded = false;
        document.body.addEventListener('htmx:beforeSend', function sent(e) {
          if (e.detail.target.id !== 'modal') return;
          e.detail.xhr.addEventListener('loadend', () => { window.__detailEnded = true; }, {once:true});
          document.body.removeEventListener('htmx:beforeSend', sent);
        });
      });
      await page.locator('#modal button', {hasText:'T+0'}).click();
      await requestedDetail;
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => window.__detailEnded);
      releaseDetail();
      const rowAfterClose = await link.elementHandle();
      await page.evaluate(() => htmx.trigger(document.getElementById('tbody-bonares'), 'tabvisible'));
      await page.waitForFunction(e => !e.isConnected, rowAfterClose);
      check(await page.locator('#modal').evaluate(e => e.children.length === 0), 'Respuesta tardía reabrió el detalle');
    } finally { releaseDetail(); await page.unroute('**/bond/*/detail?lag=0', delayedDetail); }
    result.push('cerrar detalle pendiente aborta su pedido y reanuda los refrescos');

    await page.setViewportSize({width:390, height:844});
    await page.locator('.mobile-panel-nav').waitFor({state:'visible'});
    check(await page.locator('.mobile-panel-nav').isVisible(), 'Falta navegación móvil');
    check(await page.locator('.grid-stack-item:visible').count() === 1, 'Móvil no prioriza un panel');
    await page.locator('#mobile-panel-select').selectOption('obligaciones_negociables');
    const onPanel = page.locator('[gs-id="obligaciones_negociables"]');
    check(await onPanel.isVisible(), 'No se puede llegar a ONs en móvil');
    check(await onPanel.locator('thead th:visible').count() === 4, 'Vista rápida no prioriza precio/TIR/MD');
    await onPanel.locator('.mobile-columns-toggle').click();
    check(await onPanel.locator('thead th:visible').count() > 4, 'Móvil perdió columnas completas');
    await onPanel.locator('.ley-btn[data-ley="AR"]').click();
    check(await onPanel.locator('.ley-btn[data-ley="AR"]').getAttribute('aria-pressed') === 'true', 'Filtro móvil no funciona');
    await page.locator('#mobile-panel-next').click();
    check(!await onPanel.isVisible(), 'Siguiente panel no funciona');
    await page.locator('#mobile-panel-prev').click();
    check(await onPanel.isVisible(), 'Anterior panel no funciona');
    await page.locator('.mobile-menu-trigger').click();
    for (const route of ['/on', '/fci', '/cartera', '/options']) {
      check(await page.locator('#dashboard-section-menu a[href="' + route + '"]').isVisible(), 'Menú móvil perdió ' + route);
    }
    await page.keyboard.press('Escape');
    check(!await page.locator('#dashboard-section-menu').isVisible(), 'Escape no cerró menú móvil');
    await page.locator('#mobile-panel-edit').click();
    await page.locator('.layout-visibility [data-panel="obligaciones_negociables"]').click();
    check(!await onPanel.isVisible(), 'Móvil no oculta el panel');
    await page.locator('.mobile-menu-trigger').click(); await page.keyboard.press('Escape');
    check(await page.locator('#layout-edit-bar').isVisible() && !await onPanel.isVisible(), 'Escape de Menú perdió borrador móvil');
    await page.locator('#layout-cancel').click();
    await page.locator('#mobile-panel-select').selectOption('obligaciones_negociables');
    check(await onPanel.isVisible(), 'Móvil no restaura panel al cancelar');
    equal(await page.evaluate(() => localStorage.getItem('monitor-dashboard-state-v1')), saved, 'Móvil escribió geometría de escritorio');
    await page.setViewportSize({width:1440, height:1000});
    equal(await geometry(), applied, 'Volver del móvil cambió geometría');
    result.push('navegación móvil, menús, filtros y preferencias con los mismos paneles');

    const noVendor = route => route.fulfill({contentType:'application/javascript',body:''});
    await page.route('**/static/vendor/gridstack/gridstack-all.js', noVendor);
    try {
      await page.reload();
      check(await page.locator('.dashboard-fallback').count() === 1, 'No hay fallback sin vendor');
      check(await first.locator('tbody tr').count() > 0, 'Fallback perdió los datos');
      await first.locator('[data-act="cfg"]').click();
      check(await first.locator('.col-menu').isVisible(), 'Fallback perdió los controles');
      await page.locator('#nav-config-btn').click();
      check(await page.locator('[data-action="save-default"]').isDisabled(), 'Fallback permitiría guardar una distribución vacía para todos');
    } finally { await page.unroute('**/static/vendor/gridstack/gridstack-all.js', noVendor); }
    result.push('fallback sin GridStack mantiene tablas y controles');
    equal(errors, [], 'Errores JavaScript: ' + errors.join(', '));
    return {passed: result, javascriptErrors: errors.length};
  } finally {
    page.off('pageerror', onError);
    await page.evaluate(entries => entries.forEach(([key, value]) => {
      if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value);
    }), stored);
    if (viewport) await page.setViewportSize(viewport);
    await page.reload();
  }
}
