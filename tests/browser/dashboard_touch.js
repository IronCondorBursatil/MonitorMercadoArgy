// Playwright MCP browser_run_code_unsafe({filename: <este archivo>}).
// Contexto aislado, touch emulado, templates reales y fixture sintética en :8001.
async (page) => {
  function check(condition, message) { if (!condition) throw new Error(message); }
  const health = await (await page.request.get('http://127.0.0.1:8001/api/health')).json();
  check(health.synthetic === true, 'Requiere fixture sintética');
  const context = await page.context().browser().newContext({
    viewport:{width:390,height:844}, isMobile:true, hasTouch:true, deviceScaleFactor:2,
  });
  try {
    const phone = await context.newPage();
    const errors = [];
    phone.on('pageerror', error => errors.push(String(error)));
    await phone.goto('http://127.0.0.1:8001/');
    const panel = phone.locator('[gs-id="obligaciones_negociables"]');
    await phone.locator('#mobile-panel-select').selectOption('obligaciones_negociables');
    const sizes = [];
    for (const theme of ['dark', 'light']) {
      await phone.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
      for (const viewport of [{width:320,height:740},{width:390,height:844},{width:768,height:1024},{width:844,height:390}]) {
        await phone.setViewportSize(viewport);
        await phone.locator('.mobile-panel-nav').waitFor({state:'visible'});
        const dims = await phone.evaluate(() => ({scroll:document.documentElement.scrollWidth, width:document.documentElement.clientWidth}));
        check(dims.scroll <= dims.width, 'Desborde global: ' + theme + '/' + viewport.width);
        check(await phone.locator('.grid-stack-item:visible').count() === 1, 'Se perdió el panel activo');
        sizes.push(theme + '/' + viewport.width + 'x' + viewport.height);
      }
    }
    await phone.setViewportSize({width:320,height:740});
    await phone.locator('.mobile-menu-trigger').tap();
    await phone.locator('#source-wrap summary').tap();
    const source = await phone.locator('.source-menu').boundingBox();
    check(source.x >= 0 && source.x + source.width <= 320, 'Fuente queda recortada');
    await phone.keyboard.press('Escape');
    check(await phone.locator('#dashboard-section-menu').isVisible(), 'Escape cerró dos capas');
    await phone.locator('#nav-config-btn').tap();
    await phone.keyboard.press('Escape');
    check(await phone.locator('#dashboard-section-menu').isVisible(), 'Config cerró también Menú');
    await phone.keyboard.press('Escape');
    await panel.locator('.mobile-columns-toggle').tap();
    const table = panel.locator('.panel-body');
    const before = await panel.locator('tbody tr:visible').first().locator('td').first().boundingBox();
    await table.evaluate(e => { e.scrollLeft = 140; });
    const after = await panel.locator('tbody tr:visible').first().locator('td').first().boundingBox();
    check(Math.abs(before.x - after.x) < 1, 'Ticker no queda visible al consultar otras columnas');
    await table.evaluate(e => { e.scrollLeft = 0; });
    await panel.locator('[data-act="chart"]').tap();
    await phone.locator('#curveChart').waitFor();
    const chart = await phone.locator('#curveChart').boundingBox();
    check(chart.width <= 320, 'Gráfico no entra en celular');
    await phone.locator('#modal .ph-x').tap();
    await panel.locator('[data-act="shot"]').tap();
    await phone.locator('#shareSave').waitFor();
    check(await phone.locator('#shareSave').isVisible(), 'No se puede guardar el panel');
    await phone.locator('#modal .ph-x').tap();
    await panel.locator('tbody a').first().tap();
    await phone.locator('#modal .modal-card').waitFor();
    await phone.locator('#modal button', {hasText:'T+0'}).tap();
    await phone.locator('#modal .toggle button.on', {hasText:'T+0'}).waitFor();
    await phone.locator('#modal input[name="price"]').fill('102.50');
    const input = await phone.locator('#modal input[name="price"]').evaluate(e => getComputedStyle(e).fontSize);
    check(input === '16px', 'Input propenso a zoom automático móvil');
    await phone.locator('#modal button.x').tap();
    check(await panel.isVisible(), 'Detalle perdió la familia seleccionada');
    await phone.emulateMedia({reducedMotion:'reduce'});
    check(await phone.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches), 'No emuló movimiento reducido');
    check(errors.length === 0, errors.join('\n'));
    return {viewports:sizes, touch:'menús, columnas, gráfico, compartir y detalle T+0', javascriptErrors:errors.length,
      limits:'Emulación Chromium; sin teclado virtual, hardware físico ni exportación de archivo'};
  } finally { await context.close(); }
}
