"""BCRAIndicesProvider: ventana de fetch bootstrap-vs-topup (lente largo del panel FCI)."""

from datetime import date, timedelta
from pathlib import Path

from core.infrastructure.indices_provider import BCRAIndicesProvider

W = BCRAIndicesProvider._fetch_window


def test_empty_cache_bootstraps():
    assert W({}, 400, 30) == 400


def test_short_history_backfills():
    # cache con ~7 semanas (caso real CER/A3500) → todavía pide la ventana completa
    today = date.today()
    short = {today - timedelta(days=i): 1.0 for i in range(50)}
    assert W(short, 400, 30) == 400


def test_full_history_tops_up():
    today = date.today()
    full = {today - timedelta(days=i): 1.0 for i in range(0, 400, 7)}  # ~400d cubiertos
    assert W(full, 400, 30) == 30


# ---------------------------------------------------------------------------
# Semilla versionada vs. estado de runtime
#
# POR QUE: los CSV de data/history/ estan TRACKEADOS en git, y la app los
# reescribia en cada ciclo. Resultado: el working tree del droplet quedaba sucio
# permanentemente y `git pull` abortaba en cada deploy. La acumulacion tiene que
# ir a db_dir (fuera del arbol) y la copia del repo quedar como semilla read-only.
# ---------------------------------------------------------------------------

def test_load_csv_cae_a_la_semilla_del_repo_si_no_hay_estado(tmp_path, monkeypatch):
    """Sin archivo en el state dir, _load_csv lee la semilla versionada."""
    from core.infrastructure import history_paths as hp
    from core.infrastructure import indices_provider as ip

    seed_dir, state_dir = tmp_path / "seed", tmp_path / "state"
    seed_dir.mkdir(); state_dir.mkdir()
    (seed_dir / "cer_diario.csv").write_text(
        "fecha,valor\n2026-01-02,100.5\n", encoding="utf-8")

    monkeypatch.setattr(hp, "SEED_DIR", str(seed_dir))
    data = ip._load_csv(str(state_dir / "cer_diario.csv"))

    assert data == {date(2026, 1, 2): 100.5}


def test_save_csv_no_toca_la_semilla_versionada(tmp_path, monkeypatch):
    """Escribir el estado deja la semilla del repo intacta (git pull limpio)."""
    from core.infrastructure import history_paths as hp
    from core.infrastructure import indices_provider as ip

    seed_dir, state_dir = tmp_path / "seed", tmp_path / "state"
    seed_dir.mkdir()
    seed = seed_dir / "cer_diario.csv"
    seed.write_text("fecha,valor\n2026-01-02,100.5\n", encoding="utf-8")
    antes = seed.read_bytes()

    monkeypatch.setattr(hp, "SEED_DIR", str(seed_dir))
    state = state_dir / "cer_diario.csv"
    ip._save_csv(str(state), {date(2026, 1, 2): 100.5, date(2026, 1, 3): 101.0})

    assert seed.read_bytes() == antes, "la semilla versionada fue modificada"
    assert ip._load_csv(str(state)) == {date(2026, 1, 2): 100.5, date(2026, 1, 3): 101.0}


def test_los_csv_de_runtime_apuntan_fuera_del_working_tree():
    """Los paths que la app escribe no pueden caer dentro del repo."""
    from config.settings import settings
    from core.infrastructure import indices_provider as ip

    repo = settings.base_dir.resolve()
    for path in (ip._CER_CSV, ip._TAMAR_CSV, ip._A3500_CSV, ip._RESERVAS_CSV):
        resolved = Path(path).resolve()
        assert not resolved.is_relative_to(repo / "data"), (
            f"{resolved} cae dentro de data/ (versionado): rompe git pull en el deploy")
