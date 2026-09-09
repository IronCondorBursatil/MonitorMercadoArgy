const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
global.self = global;
global.window = global;
global.GridStack = require('../../apps/web/static/vendor/gridstack/gridstack-all.js');
const source = 'apps/web/static/js/dashboard_grid.js';
if (fs.existsSync(source)) require('../../' + source);
// Antes del fix se prueba el motor que realmente usa index.html.
const Engine = global.MonitorDashboardGrid?.Engine || GridStack.Engine;
const defaults = {version: 1, layout: [
  {id: 'a', x: 0, y: 0, w: 50, h: 40},
  {id: 'b', x: 50, y: 0, w: 50, h: 40},
], hidden: []};
const copy = value => JSON.parse(JSON.stringify(value));
const geometry = e => e.nodes.map(({id, x, y, w, h}) => ({id, x, y, w, h}));

function setup() {
  const engine = new Engine({column: 200, float: true});
  defaults.layout.forEach(n => engine.addNode({...n}));
  return engine;
}

test('arrastrar sobre un vecino rechaza el movimiento y preserva ambos paneles', () => {
  const e = setup();
  e.moveNodeCheck(e.nodes[0], {x: 50, y: 0});
  assert.deepEqual(geometry(e), defaults.layout);
});

test('agrandar sobre un vecino tampoco lo empuja', () => {
  const e = setup();
  e.moveNodeCheck(e.nodes[0], {w: 80, h: 40, resizing: true});
  assert.deepEqual(geometry(e), defaults.layout);
});

test('el contacto exacto de bordes y un espacio libre admiten mover y redimensionar', () => {
  const e = setup();
  const a = e.nodes.find(n => n.id === 'a');
  e.moveNodeCheck(a, {x: 0, y: 40});
  e.moveNodeCheck(a, {w: 100, h: 60, resizing: true});
  assert.deepEqual(geometry(e).find(n => n.id === 'a'), {id: 'a', x: 0, y: 40, w: 100, h: 60});
  assert.deepEqual(geometry(e).find(n => n.id === 'b'), defaults.layout[1]);
});

test('ocultar un panel no compacta ni mueve al resto', () => {
  const e = setup();
  e.moveNodeCheck(e.nodes[1], {x: 50, y: 100});
  e.removeNode(e.nodes.find(n => n.id === 'a'));
  assert.deepEqual(geometry(e), [{id: 'b', x: 50, y: 100, w: 50, h: 40}]);
});

test('las restricciones de tamaño se evalúan antes de aceptar una posición', () => {
  const e = setup();
  const a = e.nodes[0];
  a.minW = 50;
  e.moveNodeCheck(a, {x: 10, w: 20, resizing: true});
  assert.deepEqual(geometry(e), defaults.layout);
});

test('el estado persistido rechaza colisiones, duplicados y geometría inválida', () => {
  assert.ok(global.MonitorDashboardGrid, 'falta el controlador de estado validado');
  const validate = MonitorDashboardGrid.validateState;
  assert.deepEqual(validate(defaults, ['a', 'b'], 200), defaults);
  for (const patch of [{x: 20}, {x: -1}, {w: 201}, {h: 0}, {y: 1.5}, {y: 1e9}, {id: 'a'}]) {
    const broken = copy(defaults);
    Object.assign(broken.layout[1], patch);
    assert.equal(validate(broken, ['a', 'b'], 200), null, JSON.stringify(patch));
  }
  const broken = copy(defaults);
  broken.hidden = ['unknown'];
  assert.equal(validate(broken, ['a', 'b'], 200), null);
  broken.hidden = ['a', 'a'];
  assert.equal(validate(broken, ['a', 'b'], 200), null);
});

test('un panel oculto conserva su posición aun si el hueco está ocupado', () => {
  assert.ok(global.MonitorDashboardGrid, 'falta el controlador de estado validado');
  const state = copy(defaults);
  state.layout[1].x = 0;
  state.hidden = ['b'];
  assert.deepEqual(MonitorDashboardGrid.validateState(state, ['a', 'b'], 200), state);
});

test('migra preferencias por id sin perder el panel nuevo ni reordenar los existentes', () => {
  assert.ok(global.MonitorDashboardGrid, 'falta la migración del layout anterior');
  const legacy = {layout: [{id: 'a', x: 100, y: 70, w: 50, h: 40}], hidden: ['b']};
  const state = MonitorDashboardGrid.restoreState(legacy, defaults, 200);
  assert.deepEqual(state.layout, [legacy.layout[0], {id: 'b', x: 50, y: 110, w: 50, h: 40}]);
  assert.deepEqual(state.hidden, ['b']);
  assert.equal(MonitorDashboardGrid.restoreState({layout: [{id: 'a', x: -2}]}, defaults, 200), null);
});

test('un tamaño guardado que viola minW no llega a la restauración del motor', () => {
  const state = {version: 1, layout: [
    {id: 'a', x: 0, y: 0, w: 20, h: 10}, {id: 'b', x: 20, y: 0, w: 30, h: 10},
  ], hidden: []};
  const constraints = new Map([['a', {minW: 28, minH: 8}], ['b', {minW: 28, minH: 8}]]);
  assert.equal(MonitorDashboardGrid.validateState(state, ['a', 'b'], 200, constraints), null);
  assert.equal(MonitorDashboardGrid.restoreState(state, defaults, 200, constraints), null);
});

test('sin geometría legacy utilizable se conservan los paneles ocultos válidos', () => {
  assert.equal(typeof MonitorDashboardGrid.readStoredState, 'function');
  for (const raw of [null, '{roto', JSON.stringify([{id: 'a', x: -1}])]) {
    const storage = {getItem: key => key === 'grid-layout-v3' ? raw :
      key === 'panels-hidden-v1' ? '["b"]' : null};
    const saved = MonitorDashboardGrid.readStoredState(storage, defaults, 200);
    assert.deepEqual(saved.state, {...defaults, hidden: ['b']});
  }
});
