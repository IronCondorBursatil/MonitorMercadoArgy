(function (global) {
  "use strict";

  // Adaptado del dashboard personal de David (/bonos), revisado 2026-09-09.
  // Contrato: rechazar colisiones, conservar vecinos y guardar sólo al aplicar.
  // Se usa el GridStack 11.5.1 ya vendorizado; no modifica sus archivos.

  var GridStack = global.GridStack;
  if (!GridStack || !GridStack.Engine || !GridStack.Utils) return;

  var STORAGE_KEY = "monitor-dashboard-state-v1";

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function positionsOverlap(left, right) {
    return !(left.y + left.h <= right.y || right.y + right.h <= left.y ||
             left.x + left.w <= right.x || right.x + right.w <= left.x);
  }

  function validateState(candidate, panelIds, columns, constraints) {
    if (!candidate || typeof candidate !== "object" || candidate.version !== 1 ||
        !Array.isArray(candidate.layout) || !Array.isArray(candidate.hidden) ||
        candidate.layout.length !== panelIds.length) return null;
    var known = new Set(panelIds);
    var seen = new Set();
    var layout = [];
    for (var index = 0; index < candidate.layout.length; index += 1) {
      var raw = candidate.layout[index];
      if (!raw || typeof raw !== "object" || !known.has(raw.id) || seen.has(raw.id)) return null;
      var numbers = [raw.x, raw.y, raw.w, raw.h];
      if (!numbers.every(Number.isSafeInteger) || raw.x < 0 || raw.y < 0 ||
          raw.w < 1 || raw.h < 1 || raw.x + raw.w > columns || raw.y + raw.h > 10000) return null;
      var limits = constraints && constraints.get(raw.id) || {};
      if (raw.w < (limits.minW || 1) || raw.h < (limits.minH || 1) ||
          raw.w > (limits.maxW || columns) || raw.h > (limits.maxH || 10000)) return null;
      seen.add(raw.id);
      layout.push({id: raw.id, x: raw.x, y: raw.y, w: raw.w, h: raw.h});
    }
    if (seen.size !== panelIds.length) return null;
    var hidden = new Set(candidate.hidden);
    if (hidden.size !== candidate.hidden.length) return null;
    for (var hiddenId of hidden) if (!seen.has(hiddenId)) return null;
    var visible = layout.filter(function (item) { return !hidden.has(item.id); });
    for (var left = 0; left < visible.length; left += 1) {
      for (var right = left + 1; right < visible.length; right += 1) {
        if (positionsOverlap(visible[left], visible[right])) return null;
      }
    }
    var byId = new Map(layout.map(function (item) { return [item.id, item]; }));
    return {
      version: 1,
      layout: panelIds.map(function (id) { return byId.get(id); }),
      hidden: panelIds.filter(function (id) { return hidden.has(id); }),
    };
  }

  // Migra por id: conserva preferencias válidas, ignora paneles retirados y
  // agrega los nuevos debajo. Nunca carga una geometría corrupta en el motor.
  function restoreState(candidate, defaults, columns, constraints) {
    if (!candidate || !Array.isArray(candidate.layout) ||
        !Array.isArray(candidate.hidden) ||
        (candidate.version !== undefined && candidate.version !== 1)) return null;
    var ids = defaults.layout.map(function (n) { return n.id; });
    var known = new Set(ids);
    var layout = candidate.layout.filter(function (n) { return n && known.has(n.id); }).map(clone);
    var seen = new Set(layout.map(function (n) { return n.id; }));
    var bottom = layout.reduce(function (y, n) { return Math.max(y, n.y + n.h); }, 0);
    defaults.layout.forEach(function (n) {
      if (seen.has(n.id)) return;
      var added = Object.assign({}, n, {y: bottom});
      layout.push(added);
      bottom += n.h;
    });
    return validateState({version: 1, layout: layout,
      hidden: candidate.hidden.filter(function (id) { return known.has(id); })}, ids, columns, constraints);
  }

  function readStoredState(storage, defaults, columns, constraints) {
    function read(key) {
      try { return JSON.parse(storage.getItem(key)); } catch (error) { return null; }
    }
    var raw = null;
    try { raw = storage.getItem(STORAGE_KEY); } catch (error) {}
    if (raw !== null) {
      var current = restoreState(read(STORAGE_KEY), defaults, columns, constraints);
      return {state: current || defaults, invalid: !current};
    }
    var legacyHidden = read("panels-hidden-v1");
    var hidden = Array.isArray(legacyHidden) ? legacyHidden : defaults.hidden;
    var legacy = {layout: read("grid-layout-v3"), hidden: hidden};
    return {state: restoreState(legacy, defaults, columns, constraints) ||
      restoreState({layout: defaults.layout, hidden: hidden}, defaults, columns, constraints) || defaults,
      invalid: false};
  }

  class StableGridEngine extends GridStack.Engine {
    moveNode(node, options) {
      var proposed = Object.assign({}, node);
      GridStack.Utils.copyPos(proposed, options);
      this.nodeBoundFix(proposed, proposed.w !== node.w || proposed.h !== node.h);
      if (!options.nested && !options.skip && this.collide(node, proposed)) {
        var target = node._event && node._event.target ? node._event.target : node.el;
        if (target) target.dispatchEvent(new CustomEvent("dashboard:collision", {bubbles: true}));
        return false;
      }
      return super.moveNode(node, options);
    }
  }

  function createDashboardGrid(options) {
    var element = options.element;
    var columns = options.columns;
    var items = Array.prototype.slice.call(element.querySelectorAll("[gs-id]"));
    var panelIds = items.map(function (item) { return item.getAttribute("gs-id"); });
    var itemById = new Map(items.map(function (item) {
      return [item.getAttribute("gs-id"), item];
    }));
    var constraintsById = new Map(items.map(function (item) {
      var constraints = {};
      [
        ["minW", "gs-min-w"],
        ["minH", "gs-min-h"],
        ["maxW", "gs-max-w"],
        ["maxH", "gs-max-h"],
      ].forEach(function (entry) {
        var value = Number(item.getAttribute(entry[1]));
        if (Number.isInteger(value) && value > 0) constraints[entry[0]] = value;
      });
      return [item.getAttribute("gs-id"), constraints];
    }));
    var defaultEngine = new GridStack.Engine({column: columns, float: true});
    items.forEach(function (item) {
      defaultEngine.addNode({id: item.getAttribute("gs-id"), autoPosition: true,
        w: Number(item.getAttribute("gs-w")), h: Number(item.getAttribute("gs-h"))});
    });
    var automatic = {version: 1, layout: defaultEngine.nodes.map(positionFromNode), hidden: []};
    var defaultState = restoreState(options.defaultState, automatic, columns, constraintsById) || automatic;

    var initialState = defaultState;
    var storedStateInvalid = false;
    try {
      var restored = readStoredState(global.localStorage, defaultState, columns, constraintsById);
      initialState = restored.state;
      storedStateInvalid = restored.invalid;
    } catch (error) {}
    if (storedStateInvalid) {
      if (global.console && typeof global.console.warn === "function") {
        global.console.warn("Distribución guardada inválida; se muestra la predeterminada.");
      }
    }

    // 11.5.1 hace autoscroll al estirar aun con draggable.scroll=false. El
    // adaptador se limita a este dashboard; otros grids conservan su conducta.
    if (!GridStack.Utils.__monitorResizeScroll) {
      GridStack.Utils.__monitorResizeScroll = GridStack.Utils.updateScrollResize;
      GridStack.Utils.updateScrollResize = function (event, target, distance) {
        if (target && target.closest && target.closest(".monitor-dashboard")) return;
        return GridStack.Utils.__monitorResizeScroll.call(this, event, target, distance);
      };
    }

    var grid = GridStack.init({
      column: columns,
      cellHeight: options.cellHeight,
      margin: options.margin,
      float: true,
      animate: false,
      handle: ".layout-drag-handle",
      draggable: {scroll: false},
      resizable: {handles: "e, se"},
      alwaysShowResizeHandle: true,
      engineClass: StableGridEngine,
      auto: false,
    }, element);
    var positions = new Map();
    var hiddenPanels = new Set();
    var appliedState = clone(initialState);
    var draftState = clone(initialState);
    var editSnapshot = null;
    var editing = false;
    var replacing = false;
    var lastError = storedStateInvalid ? "Se recuperó la distribución predeterminada." : null;

    function positionFromNode(node) {
      return {id: node.id, x: node.x, y: node.y, w: node.w, h: node.h};
    }

    function syncVisiblePositions() {
      grid.engine.nodes.forEach(function (node) {
        positions.set(node.id, positionFromNode(node));
      });
    }

    function captureState() {
      syncVisiblePositions();
      return {
        version: 1,
        layout: panelIds.map(function (id) { return clone(positions.get(id)); }),
        hidden: panelIds.filter(function (id) { return hiddenPanels.has(id); }),
      };
    }

    function getState() {
      return clone(editing ? draftState : appliedState);
    }

    function emit(name) {
      element.dispatchEvent(new CustomEvent(name, {
        detail: {editing: editing, state: getState(), error: lastError},
      }));
    }

    function makeItemWidget(id, widgetOptions) {
      var item = itemById.get(id);
      grid.makeWidget(item, Object.assign({}, widgetOptions, constraintsById.get(id)));
      return item;
    }

    function replaceState(next, notify) {
      var normalized = validateState(next, panelIds, columns, constraintsById);
      if (!normalized) return false;
      var hidden = new Set(normalized.hidden);
      replacing = true;
      grid.batchUpdate();
      try {
        grid.removeAll(false, false);
        positions.clear();
        hiddenPanels.clear();
        normalized.layout.forEach(function (position) {
          positions.set(position.id, clone(position));
        });
        normalized.hidden.forEach(function (id) {
          hiddenPanels.add(id);
        });
        panelIds.forEach(function (id) {
          var item = itemById.get(id);
          if (hidden.has(id)) {
            item.hidden = true;
            return;
          }
          item.hidden = false;
          makeItemWidget(id, positions.get(id));
        });
      } finally {
        grid.batchUpdate(false);
        replacing = false;
      }
      syncVisiblePositions();
      draftState = captureState();
      if (notify !== false) emit("dashboard:statechange");
      return true;
    }

    function writeAppliedState(next) {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
        return true;
      } catch (error) {
        return false;
      }
    }

    function beginEdit() {
      if (editing) return false;
      editSnapshot = captureState();
      draftState = clone(editSnapshot);
      editing = true;
      lastError = null;
      if (!isCompact()) grid.enable();
      element.classList.add("layout-editing");
      emit("dashboard:modechange");
      return true;
    }

    function finishEdit() {
      editing = false;
      editSnapshot = null;
      draftState = clone(appliedState);
      lastError = null;
      grid.disable();
      element.classList.remove("layout-editing");
      emit("dashboard:modechange");
    }

    function cancelEdit() {
      if (!editing) return;
      replaceState(editSnapshot);
      finishEdit();
    }

    function applyEdit() {
      if (!editing) return false;
      var next = validateState(captureState(), panelIds, columns, constraintsById);
      if (!next || !writeAppliedState(next)) {
        lastError = "No se pudo guardar en este navegador. Los cambios siguen sin aplicar.";
        emit("dashboard:applyerror");
        return false;
      }
      appliedState = clone(next);
      finishEdit();
      return true;
    }

    function previewDefault() {
      if (!editing) return false;
      return replaceState(defaultState);
    }

    function setPanelVisible(id, visible) {
      if (!editing || !itemById.has(id)) return false;
      var shouldShow = Boolean(visible);
      var isHidden = hiddenPanels.has(id);
      if (shouldShow === !isHidden) return true;
      var item = itemById.get(id);
      replacing = true;
      try {
        if (shouldShow) {
          var position = clone(positions.get(id));
          var widgetOptions = position;
          if (!grid.engine.isAreaEmpty(position.x, position.y, position.w, position.h)) {
            widgetOptions = Object.assign({}, position, {autoPosition: true});
          }
          hiddenPanels.delete(id);
          item.hidden = false;
          makeItemWidget(id, widgetOptions);
          positions.set(id, positionFromNode(item.gridstackNode));
        } else {
          positions.set(id, positionFromNode(item.gridstackNode));
          hiddenPanels.add(id);
          grid.removeWidget(item, false);
          item.hidden = true;
        }
      } finally {
        replacing = false;
      }
      draftState = captureState();
      emit("dashboard:statechange");
      return true;
    }

    function isPanelHidden(id) {
      return hiddenPanels.has(id);
    }

    function isEditing() {
      return editing;
    }

    // La vista móvil es CSS, nunca escribe coordenadas sobre el layout de escritorio.
    function isCompact() {
      return global.matchMedia("(max-width: 900px)").matches;
    }

    function movePanel(id, change) {
      if (!editing || isCompact() || hiddenPanels.has(id) || !itemById.has(id)) return false;
      var item = itemById.get(id);
      var node = item.gridstackNode;
      var next = Object.assign({}, positionFromNode(node), change);
      grid.engine.nodeBoundFix(next, next.w !== node.w || next.h !== node.h);
      if (grid.engine.collide(node, next)) {
        item.dispatchEvent(new CustomEvent("dashboard:collision", {bubbles: true}));
        return false;
      }
      grid.update(item, next);
      return true;
    }

    replaceState(initialState, false);
    appliedState = clone(captureState());
    draftState = clone(appliedState);
    grid.on("change", function () {
      if (replacing) return;
      draftState = captureState();
      emit("dashboard:statechange");
    });
    grid.disable();
    element.classList.add("dashboard-ready");
    global.matchMedia("(max-width: 900px)").addEventListener("change", function () {
      if (editing) cancelEdit();
      emit("dashboard:modechange");
    });

    var controller = {
      grid: grid,
      beginEdit: beginEdit,
      applyEdit: applyEdit,
      cancelEdit: cancelEdit,
      previewDefault: previewDefault,
      setPanelVisible: setPanelVisible,
      isPanelHidden: isPanelHidden,
      isEditing: isEditing,
      getState: getState,
      isCompact: isCompact,
      movePanel: movePanel,
    };
    Object.defineProperty(controller, "lastError", {
      enumerable: true,
      get: function () { return lastError; },
    });
    return controller;
  }

  global.MonitorDashboardGrid = {create: createDashboardGrid, validateState: validateState,
    restoreState: restoreState, readStoredState: readStoredState, Engine: StableGridEngine};
})(window);
