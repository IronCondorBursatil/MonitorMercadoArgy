# Se instala en /etc/profile.d/monitor.sh
#
# `MONITOR_DB_DIR` vive en el unit de systemd, y un `venv/bin/python scripts/...`
# lanzado desde una shell interactiva NO lo hereda: `db_dir` cae al default de Linux
# (~/.local/share/monitor, vacío) y el script opera sobre la base equivocada sin
# decirlo. Le pasó a `backfill_tamar_anchor.py` —que desde 2026-09 imprime la ruta y
# aborta si el catálogo está vacío— y le sigue pasando a `bench_pricing.py` y
# `bench_churn.py`, que no tienen ese guard.
#
# Sólo para shells de login: los timers y el servicio traen la variable explícita.
export MONITOR_DB_DIR=/var/lib/monitor
