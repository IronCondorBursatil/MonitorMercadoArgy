"""Cache-Control de los estáticos: sin esto el navegador revalida ~556KB de
vendor (chart/gridstack/htmx/html2canvas) en CADA navegación de página completa,
lo que hace lento el cambio de pestañas contra un droplet con latencia. Los
vendor son inmutables → cache eterno; el CSS/JS propio se sirve con cache-busting
(`?v=`) que también habilita el cache eterno para la URL versionada."""

from fastapi.testclient import TestClient

from apps.web.app import app


def test_vendor_assets_are_immutable():
    with TestClient(app) as client:
        r = client.get("/static/vendor/htmx.min.js")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "immutable" in cc, f"vendor sin immutable: {cc!r}"
    assert "max-age=31536000" in cc, f"vendor sin max-age largo: {cc!r}"


def test_versioned_asset_is_immutable():
    """Una URL con `?v=<hash/mtime>` es cache-busting → puede cachearse para siempre."""
    with TestClient(app) as client:
        r = client.get("/static/css/app.css?v=123")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "immutable" in cc, f"asset versionado sin immutable: {cc!r}"


def test_unversioned_own_asset_has_short_revalidation():
    """CSS/JS propio SIN `?v=` no debe ser immutable (un deploy tiene que verse);
    se sirve con cache corto para no romper la vista tras un deploy."""
    with TestClient(app) as client:
        r = client.get("/static/css/app.css")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "immutable" not in cc, f"asset propio no versionado NO debe ser immutable: {cc!r}"
    assert "max-age" in cc, f"asset propio sin política de cache: {cc!r}"
