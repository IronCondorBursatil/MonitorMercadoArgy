"""Preview efímero: templates reales, todas las filas y respuestas sintéticas."""
import sys
import os
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import json
import mimetypes
import time
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_sandbox = tempfile.TemporaryDirectory(prefix='monitor-ui-fixture-')
os.environ['MONITOR_TEST_DB_DIR'] = _sandbox.name
import tests.conftest  # noqa: E402, F401 — aislamiento ANTES de importar config/dominio
from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402
from apps.web.routers.panels_schema import (  # noqa: E402
    PANELS, PANEL_ORDER, CCY_FILTER_PANELS, SETTLE_FILTER_PANELS,
    LEY_FILTER_PANELS, _HL_COL_KEY,
)
from apps.web.panels_rows import panel_columns  # noqa: E402

env = Environment(loader=FileSystemLoader(ROOT / 'apps/web/templates'), autoescape=select_autoescape())
def panels():
    return [dict(id=pid, title=PANELS[pid][0], columns=panel_columns(pid),
        ccy_filter=pid in CCY_FILTER_PANELS, ley_filter=pid in LEY_FILTER_PANELS,
        settle_filter=pid in SETTLE_FILTER_PANELS, hl_key=_HL_COL_KEY.get(pid),
        chartable=bool(PANELS[pid][1]), rows=rows(pid)) for pid in PANEL_ORDER]

def rows(pid):
    result = []
    for i in range(25 if pid in ('cer', 'obligaciones_negociables') else 10):
        cells = []
        for column in panel_columns(pid):
            key = column['key']
            value = {'ticker': f'TEST{i:02}D', 'vto': '30/06/28', 'category': 'Instrumento sintético',
                'tir': '8.25%', 'price': '101.25', 'duration': '2.10', 'parity': '99.90%',
                'change_pct': '+0.25%', 'volume': '1.2M'}.get(key, '1.25')
            cells.append(dict(text=value, cls='num' if key!='ticker' else '', key=key))
        result.append(dict(ticker=f'TEST{i:02}D', ccy='MEP', ley='AR', cells=cells))
    return result

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        code = 200
        mime = 'text/html; charset=utf-8'
        if path == '/':
            body = env.get_template('pages/index.html').render(panels=panels(),
                last_refresh=datetime(2026,9,9,12,0), has_tab=lambda tab: True,
                current_user=SimpleNamespace(username='prueba-sintetica', is_admin=True), default_layout='null').encode()
        elif path.startswith('/static/'):
            file = (ROOT / 'apps/web/static' / path.removeprefix('/static/')).resolve()
            if not file.is_relative_to((ROOT / 'apps/web/static').resolve()) or not file.is_file():
                self.send_error(404); return
            mime = mimetypes.guess_type(file)[0] or 'application/octet-stream'
            body = file.read_bytes()
        elif path.startswith('/panels/') and path.endswith('/rows'):
            pid = path.split('/')[2]
            body = env.get_template('fragments/panel_rows.html').render(rows=rows(pid), ncols=len(panel_columns(pid))).encode()
        elif path.startswith('/panels/') and path.rsplit('/', 1)[-1] in ('chart', 'share'):
            pid, kind = path.split('/')[2:4]
            datasets = [dict(label='Prueba', color='#2962ff', points=[
                dict(x=1+i, y=8+i, ticker=f'TEST{i:02}D') for i in range(3)], curve=[])]
            body = env.get_template(f'fragments/panel_{kind}.html').render(title=PANELS[pid][0],
                y_label='TIR', datasets_json=json.dumps(datasets), has_chart=True, badge='Prueba',
                columns=panel_columns(pid), rows=rows(pid), hl_key=_HL_COL_KEY.get(pid)).encode()
        elif path == '/health/badge':
            body = b'<span class="meta">VISTA DE PRUEBA - datos sinteticos</span>'
        elif path == '/header/cards':
            body = b'<span class="hstrip-loading">Datos sinteticos para verificar layout. No son cotizaciones.</span>'
        elif path == '/source/menu':
            body = env.get_template('fragments/source_menu.html').render(label='Prueba', active='fixture',
                modes=[dict(mode='fixture', label='Datos sintéticos', available=True)], realtime_ready=False).encode()
        elif path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b'event: refresh\ndata: fixture\n\n')
                    self.wfile.flush()
                    time.sleep(5)
            except (ConnectionError, OSError):
                return
        elif path.startswith('/bond/'):
            lag = parse_qs(urlparse(self.path).query).get('lag', ['1'])[0]
            metrics = dict.fromkeys(('tir tna_nominal current_yield duration technical_value parity '
                'accrued_interest days_accrued tna tem tna_mensual tem_360 residual_nominal dv01 '
                'convexity spread_mercado').split())
            metrics.update(tir=.0825, duration=2.1, parity=.999)
            detail = dict(ticker='TEST00D', settlement_lag=int(lag), settle_date='2026-09-10',
                market=dict(price=101.25), metrics=metrics, cashflows=[], meta=dict(short_name='Bono sintético',
                    instrument_type='BONO', currency='USD', fecha_emision='2024-01-01',
                    fecha_vencimiento='2028-06-30', payment_frequency=2, cupon='8%'))
            body = env.get_template('fragments/bond_detail.html').render(d=detail, lag=int(lag)).encode()
        elif path == '/api/health':
            mime = 'application/json'
            body = json.dumps({'status':'fixture', 'instruments':len(PANEL_ORDER), 'synthetic':True}).encode()
        else:
            self.send_error(404); return
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

if __name__ == '__main__':
    # Puerto fijo del protocolo /smoke. El llamador comprueba que esté libre y
    # detiene este proceso al terminar; no iniciar junto al server real.
    with ThreadingHTTPServer(('127.0.0.1', 8001), Handler) as server:
        try:
            server.serve_forever()
        finally:
            _sandbox.cleanup()
