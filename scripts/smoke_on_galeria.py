"""Smoke test del bundle de 1 archivo (on-galeria.html) con Playwright — foco en el mock 21.

Carga on-galeria.html por file://, abre la galería, entra al mock combinado #21 (Mesa ON ·
3 vistas) y ejercita las 3 subpestañas + facetas + comparador + cuadrantes, exigiendo CERO
errores de consola. Verifica además que un mock previo (20) siga abriendo.

    py -3.12 scripts/smoke_on_galeria.py
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "mockups" / "on-galeria.html"
URL = OUT.as_uri()

errors = []


def find_frame(page, marker, tries=80):
    """Devuelve el iframe (about:blank escrito por document.write) que contiene `marker`."""
    for _ in range(tries):
        for f in page.frames:
            if f == page.main_frame:
                continue
            try:
                if f.locator(marker).count() > 0:
                    return f
            except Exception:
                pass
        page.wait_for_timeout(100)
    return None


def open_mock(page, num, marker):
    page.locator('#on-gallery-root a.card[href*="%s-"]' % num).first.click()
    page.wait_for_selector("#on-viewer:not([hidden])", timeout=5000)
    frame = find_frame(page, marker)
    assert frame, "[%s] no apareció el iframe con %s" % (num, marker)
    return frame


def close_viewer(page):
    # botón del padre (Escape no llega al padre si el foco quedó dentro del iframe)
    page.locator("#on-view-back").click()
    page.locator("#on-viewer").wait_for(state="hidden", timeout=5000)


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("console", lambda m: errors.append("console.error: " + m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))

        page.goto(URL, wait_until="networkidle")
        page.wait_for_selector("header.on-header", timeout=5000)
        n_cards = page.locator("#on-gallery-root a.card").count()
        print("galería: %d cards" % n_cards)
        assert n_cards == 21, "esperaba 21 cards, hay %d" % n_cards

        # ---- mock 21 ----
        fr = open_mock(page, "21", "#subtab-bar")
        for sel in ["#sidebar", "#subtab-bar", "#tab-sectores .scard", "#sec-liga-body tr.sect-row", "#sec-hm-body tr.emit-row"]:
            fr.wait_for_selector(sel, timeout=8000)
        n_scards = fr.locator("#tab-sectores .scard").count()
        n_liga = fr.locator("#sec-liga-body tr.sect-row").count()
        n_hm = fr.locator("#sec-hm-body tr.emit-row").count()
        print("  21/sectores: scards=%d liga=%d heatmap=%d" % (n_scards, n_liga, n_hm))
        assert n_scards > 0 and n_liga > 0 and n_hm > 0

        # facet re-render del tab activo: apago Ley EXT y miro que cambie el KPI compartido
        kpi_before = fr.locator("#kpi-n").inner_text()
        fr.locator("input[name='ley'][value='EXT']").uncheck()
        fr.wait_for_timeout(250)
        kpi_after = fr.locator("#kpi-n").inner_text()
        print("  21/faceta Ley EXT off: kpi-n %s -> %s" % (kpi_before, kpi_after))
        assert kpi_before != kpi_after, "el KPI no cambió al togglear faceta"
        fr.locator("input[name='ley'][value='EXT']").check()
        fr.wait_for_timeout(200)

        # expandir un sector de la liga
        fr.locator("#sec-liga-body tr.sect-row").first.click()
        fr.wait_for_selector("#sec-liga-body .bond-inner.open table.sub", timeout=4000)
        print("  21/liga: sub-tabla expandida ok")

        # subpestaña Cards + Comparador
        fr.locator('.subtab[data-tab="cards"]').click()
        fr.wait_for_selector("#tab-cards:not([hidden]) #deck-grid .bond-card", timeout=8000)
        n_deck = fr.locator("#deck-grid .bond-card").count()
        assert n_deck > 0, "deck vacío"
        cards = fr.locator("#deck-grid .bond-card")
        cards.nth(0).click(); cards.nth(1).click(); cards.nth(2).click()
        fr.wait_for_timeout(150)
        cnt = fr.locator("#cmp-count strong").inner_text()
        n_sel = fr.locator("#deck-grid .bond-card.selected").count()
        has_table = fr.locator("#cmp-table-area table.cmp").count()
        print("  21/cards: deck=%d seleccionadas=%s cmp-count=%s tabla=%d" % (n_deck, n_sel, cnt, has_table))
        assert cnt == "3" and n_sel == 3 and has_table == 1, "comparador no refleja la selección"
        fr.locator("#cmp-chips-area .sel-chip").first.click()
        fr.wait_for_timeout(150)
        cnt2 = fr.locator("#cmp-count strong").inner_text()
        assert cnt2 == "2", "quitar chip no decrementó (%s)" % cnt2
        print("  21/cards: quitar chip -> %s ok" % cnt2)

        # subpestaña Dashboard + Cuadrantes
        fr.locator('.subtab[data-tab="dash"]').click()
        fr.wait_for_selector("#tab-dash:not([hidden]) #dash-quad-kpis .q-kpi", timeout=8000)
        n_q = fr.locator("#dash-quad-kpis .q-kpi").count()
        assert n_q == 4, "esperaba 4 q-kpi, hay %d" % n_q
        assert fr.locator("#dash-quad-canvas").count() == 1
        movers_before = fr.locator("#dash-tbody-movers tr").count()
        fr.locator("#dash-quad-kpis .q-kpi").first.click()
        fr.wait_for_timeout(200)
        active = fr.locator("#dash-quad-kpis .q-kpi.active").count()
        badge = fr.locator("#dash-q-badge .q-badge").count()
        movers_after = fr.locator("#dash-tbody-movers tr").count()
        print("  21/dash: q-kpi=%d active=%d badge=%d movers %d->%d" % (n_q, active, badge, movers_before, movers_after))
        assert active == 1 and badge == 1, "click de cuadrante no activó el filtro"
        fr.locator("#dash-quad-kpis .q-kpi").first.click()  # limpiar
        fr.wait_for_timeout(150)
        assert fr.locator("#dash-quad-kpis .q-kpi.active").count() == 0, "no se limpió el cuadrante"

        # toggle de tema dentro del iframe
        fr.locator("header.on-header .tbtn").click()
        fr.wait_for_timeout(250)
        assert fr.locator("#dash-quad-canvas").count() == 1, "el canvas desapareció tras cambiar tema"
        print("  21/tema: toggle ok, canvas presente")

        close_viewer(page)

        # ---- regresión: el mock 20 sigue abriendo ----
        fr20 = open_mock(page, "20", "#scatter-canvas")
        fr20.wait_for_function("window.ON && window.ON_DATA && window.ON_DATA.bonds.length > 0", timeout=5000)
        print("  20/regresión: abre ok")
        close_viewer(page)

        browser.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # noqa: BLE001
        errors.append("EXCEPTION: " + str(e))
    if errors:
        print("\n--- PROBLEMAS ---")
        for e in errors:
            print("  ", e)
        sys.exit(1)
    print("\nOK: galería (21) + mock 21 (3 subpestañas, facetas, comparador, cuadrantes) sin errores de consola.")
