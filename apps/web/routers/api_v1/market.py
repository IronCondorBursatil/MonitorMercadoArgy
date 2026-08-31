from fastapi import APIRouter, Depends
from typing import List
from datetime import date

from apps.web.deps import get_state
from apps.web.routers.panels_schema import PANELS, PRICE_REQUIRED_PANELS
from apps.web.panels_rows import _row_values

router = APIRouter()

def build_market_json(panel_id: str, state) -> List[dict]:
    if panel_id not in PANELS:
        return []

    _title, types, _cols = PANELS[panel_id]
    today = date.today()
    price_required = panel_id in PRICE_REQUIRED_PANELS

    metrics = [m for m in state.metrics()
               if m.snapshot and m.snapshot.instrument and m.snapshot.instrument.instrument_type in types]

    def _sort_key(m):
        base_dur = m.duration or 0.0
        if panel_id == "bonares" and m.snapshot and m.snapshot.instrument:
            tk = m.snapshot.instrument.ticker.upper()
            if tk.startswith("AO") or tk.startswith("AN"):
                group = 0
            elif tk.startswith("AL") or tk.startswith("AE"):
                group = 1
            elif tk.startswith("GD"):
                group = 2
            else:
                group = 3
            return (m.duration is None, group, base_dur)
        return (m.duration is None, base_dur)

    metrics.sort(key=_sort_key)

    rows = []
    for m in metrics:
        if price_required and not m.snapshot.price:
            continue
        vals = _row_values(m, today)

        # Add bid/ask for React to use directly
        vals["px_bid"] = m.snapshot.bid
        vals["px_ask"] = m.snapshot.ask
        vals["c"] = m.snapshot.price
        vals["v"] = m.snapshot.volume

        rows.append(vals)

    return rows

@router.get("/{panel_id}")
def get_market_data(panel_id: str, state=Depends(get_state)):
    return build_market_json(panel_id, state)
