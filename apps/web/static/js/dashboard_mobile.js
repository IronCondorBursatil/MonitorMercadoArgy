/* Mejora progresiva: un panel por vez en celular, sobre el DOM SSR compartido. */
(function () {
  "use strict";
  var grid = document.querySelector(".monitor-dashboard");
  if (!grid) return;
  var media = window.matchMedia("(max-width: 900px)");
  var nav = document.querySelector(".mobile-panel-nav");
  var select = document.getElementById("mobile-panel-select");
  var prev = document.getElementById("mobile-panel-prev");
  var next = document.getElementById("mobile-panel-next");
  var count = document.getElementById("mobile-panel-count");
  var items = Array.from(grid.querySelectorAll(".grid-stack-item"));
  var active;
  var summaryCSS = [];
  items.forEach(function (item) {
    var panel = item.querySelector(".panel");
    var heads = Array.from(panel.querySelectorAll("thead th"));
    var highlighted = panel.querySelector("thead th.tircol");
    var keys = ["ticker", "price", highlighted ? highlighted.dataset.key : "tir", "duration"];
    var essential = heads.filter(function (head) { return keys.indexOf(head.dataset.key) !== -1; });
    var button = panel.querySelector(".mobile-columns-toggle");
    // En paneles especiales sin ese schema se mantiene la tabla completa.
    if (essential.length < 3) { button.hidden = true; return; }
    panel.classList.add("mobile-summary");
    heads.forEach(function (head, index) {
      if (essential.indexOf(head) !== -1) return;
      var selector = '.monitor-dashboard [data-id="' + CSS.escape(panel.dataset.id) + '"].mobile-summary';
      summaryCSS.push(selector + ' th:nth-child(' + (index + 1) + '),' + selector + ' td:nth-child(' + (index + 1) + '){display:none}');
    });
    function setSummary(summary) {
      panel.classList.toggle("mobile-summary", summary);
      button.textContent = summary ? "Todas las columnas" : "Vista rápida";
      button.setAttribute("aria-pressed", String(!summary));
    }
    button.addEventListener("click", function () { setSummary(!panel.classList.contains("mobile-summary")); });
    panel.querySelector('[data-act="cfg"]').addEventListener("click", function () { setSummary(false); });
  });
  var style = document.createElement("style");
  style.textContent = '@media(max-width:900px){' + summaryCSS.join('') + '}';
  document.head.appendChild(style);
  try { active = sessionStorage.getItem("monitor-active-panel"); } catch (e) {}
  function visibleItems() { return items.filter(function (item) { return !item.hidden; }); }
  function sync(refresh) {
    var visible = visibleItems();
    var current = visible.find(function (item) { return item.getAttribute("gs-id") === active; }) || visible[0];
    active = current ? current.getAttribute("gs-id") : "";
    items.forEach(function (item) { item.classList.toggle("mobile-panel-inactive", item !== current); });
    Array.from(select.options).forEach(function (option) {
      option.hidden = !visible.some(function (item) { return item.getAttribute("gs-id") === option.value; });
      option.disabled = option.hidden;
    });
    select.value = active;
    select.disabled = !current;
    var index = visible.indexOf(current);
    prev.disabled = index <= 0;
    next.disabled = index < 0 || index === visible.length - 1;
    count.textContent = current ? (index + 1) + " de " + visible.length : "0 de 0";
    nav.hidden = !media.matches;
    document.querySelector(".mobile-panel-empty").hidden = !media.matches || !!current;
    if (media.matches && refresh && current && window.htmx) {
      htmx.trigger(current.querySelector("tbody"), "tabvisible");
    }
    try { sessionStorage.setItem("monitor-active-panel", active); } catch (e) {}
  }
  function choose(id) { active = id; sync(true); }
  select.addEventListener("change", function () { choose(select.value); });
  [prev, next].forEach(function (button, i) {
    button.addEventListener("click", function () {
      var visible = visibleItems();
      var index = visible.findIndex(function (item) { return item.getAttribute("gs-id") === active; });
      var target = visible[index + (i === 0 ? -1 : 1)];
      if (target) choose(target.getAttribute("gs-id"));
    });
  });
  grid.addEventListener("dashboard:statechange", function () { sync(true); });
  media.addEventListener("change", function () {
    sync(true); closeMenu();
    // Al volver a desktop, reactivar también los paneles que no se estaban mirando.
    if (!media.matches && window.htmx) htmx.trigger(document.body, "tabvisible");
  });

  // Se conserva la navegación original, incluidos permisos, fuente y cierre de sesión.
  var header = document.querySelector("body > header");
  var menu = header.querySelector("nav");
  menu.id = "dashboard-section-menu";
  menu.setAttribute("aria-label", "Secciones del Monitor");
  var trigger = document.createElement("button");
  trigger.type = "button"; trigger.className = "mobile-menu-trigger";
  trigger.textContent = "Menú";
  trigger.setAttribute("aria-controls", menu.id);
  trigger.setAttribute("aria-expanded", "false");
  header.querySelector(".header-top").insertBefore(trigger, menu);
  function closeMenu() {
    header.classList.remove("mobile-menu-open"); trigger.setAttribute("aria-expanded", "false");
  }
  trigger.addEventListener("click", function () {
    var open = !header.classList.contains("mobile-menu-open");
    header.classList.toggle("mobile-menu-open", open);
    trigger.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", function (event) {
    if (!menu.contains(event.target) && event.target !== trigger) closeMenu();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !event.defaultPrevented && header.classList.contains("mobile-menu-open") &&
        !document.querySelector(".col-menu.open, .nav-cfg-wrap.open, #modal > *")) {
      closeMenu(); trigger.focus();
    }
  });
  header.querySelectorAll("nav > a").forEach(function (link) {
    if (link.getAttribute("href") === "/") link.setAttribute("aria-current", "page");
  });
  document.body.classList.add("dashboard-mobile-ready");
  sync(false);
})();
