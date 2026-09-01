"""Calificaciones crediticias por emisor (FIX SCR, escala nacional argentina).

Data de referencia a nivel EMISOR (no por instrumento). Dos fuentes que se MERGEAN:
`data/calificaciones.csv` (la semilla, cargada a mano) y el último corte del listado
público de FIX que guarda `ratings_history` (el dato vivo, que refresca el loop diario).
El corte pisa emisor por emisor; el CSV NO se descarta cuando hay corte porque FIX deja de
publicar emisores (Agrality, Metalfor: calificación retirada) y un fallback "store si hay
corte, si no CSV" los borraría del panel de un día para el otro.

El join contra el dataset de /on es por nombre de emisor con un matcher normalizado
tolerante: ignora mayúsculas/acentos, sufijos societarios (S.A./S.A.I.C./S.R.L.…), el
sufijo "- Clase X" de las altas ABM, y matchea por acrónimo entre paréntesis
(EDENOR/EDESA/EDEMSA/CGC/PAME/PCR/SAMI/SAIEP/EPEC).

Cada nombre produce VARIOS cores: el del nombre "pelado" MÁS uno por cada grupo entre
paréntesis (así "YPF Energía Eléctrica S.A. (YPF Luz)" también se conoce como "YPF Luz",
y "Tango Energy … (Ex Petrolera Aconcagua Energía S.A.)" como "Petrolera Aconcagua
Energía"). El catálogo suele traer el nombre COMERCIAL corto ("IRSA") y el listado FIX el
legal largo ("IRSA Inversiones y Representaciones S.A."), así que la contención se evalúa
en AMBAS direcciones — pero exigiendo ganador ÚNICO por especificidad: ante empate no se
devuelve nada, porque un rating equivocado es peor que ningún rating.

`rating_for(emisor)` → {rating, perspectiva, sector, emisor, grade, as_of, source} o None.
`as_of()` es la fecha del dato vigente (corte del store, o el AS_OF del CSV si no hay).
"""
from __future__ import annotations

import csv
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

CSV_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "calificaciones.csv"
AS_OF = "2026-08-31"     # corte de la SEMILLA (el vivo lo da as_of(); ver docstring)
SOURCE = "FIX SCR"

# formas societarias / conectores / país que NO discriminan al emisor (se descartan del core)
_STOP = {
    "sa", "sau", "srl", "se", "sl", "saic", "saci", "sacif", "saicif", "saai", "ciasa",
    "agicif", "llc", "scia", "cia", "financiera", "sucursal", "argentina", "compania",
    "sociedad", "anonima", "de", "del", "la", "las", "los", "el", "y", "e", "ord", "hnos",
    "com", "ex",
}
_CORP = {"SA", "SAU", "SRL", "SE", "SL", "SAIC", "SACI", "SACIF", "SAICIF", "SAAI", "CIASA", "LLC", "AGICIF"}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _core(text: str) -> FrozenSet[str]:
    """Tokens significativos de un fragmento (sin acentos, sin formas societarias)."""
    flat = re.sub(r"[^a-z0-9]+", " ", _strip_accents(text).lower())
    return frozenset(t for t in flat.split() if len(t) > 1 and t not in _STOP)


def normalize_core(name: str) -> Tuple[Tuple[FrozenSet[str], ...], Set[str]]:
    """(cores del nombre, acrónimos en MAYÚSCULA de los paréntesis).

    `cores[0]` es el core del nombre sin los paréntesis; los siguientes son un core
    por cada grupo entre paréntesis (nombre comercial / ex-denominación). Los cores
    vacíos y los duplicados se descartan."""
    name = re.sub(r"\s*-\s*clase\b.*$", "", name or "", flags=re.I)  # altas ABM: "… - Clase X"
    acrs: Set[str] = set()
    aliases: List[FrozenSet[str]] = []
    for inside in re.findall(r"\(([^)]*)\)", name):
        toks = re.findall(r"[A-Za-z]+", inside.replace(".", ""))
        non_corp = [t for t in toks if t.upper() not in _CORP]
        if len(non_corp) == 1 and non_corp[0].isupper() and 2 <= len(non_corp[0]) <= 7:
            acrs.add(non_corp[0])
        aliases.append(_core(inside))
    cores: List[FrozenSet[str]] = []
    for c in [_core(re.sub(r"\([^)]*\)", " ", name)), *aliases]:
        if c and c not in cores:
            cores.append(c)
    return tuple(cores), acrs


# --------------------------------------------------------------------------- #
# Padrón vigente = CSV semilla + último corte del store (merge por emisor)
# --------------------------------------------------------------------------- #
def _store():
    """Store del historial FIX. Import perezoso: `ratings.py` tiene que poder importarse
    (y matchear) sin tocar SQLite — lo usan scripts y tests sin DB."""
    from core.infrastructure.ratings_history import get_ratings_history_store
    return get_ratings_history_store()


def _corte_vigente() -> Optional[str]:
    """Fecha ISO del último corte guardado, o None si no hay (o si el store no responde).

    Un problema del store NUNCA puede dejar al panel sin calificaciones: se loguea y se
    sigue con la semilla."""
    try:
        return _store().latest_fecha()
    except Exception as e:      # noqa: BLE001 — degradar a CSV, no romper el panel
        logger.warning("ratings: no se pudo leer el corte FIX (%s); se sirve el CSV", e)
        return None


def as_of() -> str:
    """Fecha de vigencia del dato que muestra el panel: la del último corte, o la del CSV.

    Reemplaza a la constante `AS_OF`, que quedó como el corte de la SEMILLA (y como
    fallback): con el loop diario la fecha ya no se edita a mano."""
    return _corte_vigente() or AS_OF


def _csv_entries() -> List[Dict]:
    """Entradas de la semilla. Se reconstruye en cada cambio de corte (1×/día): el merge
    MUTA las entradas, así que no puede compartir objetos entre cortes."""
    out: List[Dict] = []
    if not CSV_PATH.exists():
        return out
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            cores, acrs = normalize_core(r["emisor"])
            out.append({"emisor": r["emisor"].strip(), "sector": r["sector"].strip(),
                        "rating": r["rating"].strip(), "perspectiva": r["perspectiva"].strip(),
                        "cores": cores, "acrs": acrs, "as_of": AS_OF})
    return out


def _merge_corte(base: List[Dict]) -> None:
    """Pisa `base` con el último corte, emisor por emisor (in-place).

    Tres decisiones del merge:

    1. **Sin contención** (`allow_containment=False`): ese nivel del matcher es una
       heurística pensada para la CONSULTA, donde la alternativa es quedarse sin rating.
       Acá la alternativa es tener dos entradas —que el propio matcher desempata— mientras
       que colapsar de más le pondría a un emisor el rating de otro ("Tecpetrol
       Internacional" sobre "Tecpetrol S.A."): silenciosamente equivocado.
    2. **El emisor canónico pasa a ser la entidad de FIX**: es la clave con la que
       `fix_changes` guarda el cambio, y el badge del panel joinea por ahí. Los cores y
       acrónimos se UNEN con los del CSV para no perder poder de matcheo (el alias
       "Petrolera Aconcagua Energía" sólo vive en la semilla).
    3. **El sector del CSV le gana al del corte**: FIX publica una taxonomía gruesa
       ("Energia") y la del CSV está curada ("Generación Eléctrica"). El corte aporta
       rating, perspectiva y fecha; no taxonomía.

    La perspectiva del store llega YA normalizada al vocabulario del CSV ("Estable",
    "N/A", "RW Positivo") — re-traducirla acá haría que el primer diff marcara cambios
    de perspectiva falsos en todo el panel.
    """
    try:
        rows = _store().latest_entries()
    except Exception as e:      # noqa: BLE001 — ver _corte_vigente
        logger.warning("ratings: corte FIX ilegible (%s); se sirve el CSV", e)
        return
    for r in rows:
        emisor = str(r.get("emisor") or r.get("entidad") or "").strip()
        rating = str(r.get("rating") or "").strip()
        if not emisor or not rating:      # sin rating LP no aporta nada al panel
            continue
        cores, acrs = normalize_core(emisor)
        if not cores and not acrs:
            continue
        fresh = {
            "emisor": emisor,
            "sector": str(r.get("sector") or "").strip(),
            "rating": rating,
            "perspectiva": str(r.get("perspectiva") or "").strip(),
            "cores": cores,
            "acrs": acrs,
            "as_of": str(r.get("fecha_corte") or "").strip() or AS_OF,
        }
        prev = _match(cores, acrs, base, allow_containment=False)
        if prev is None:
            base.append(fresh)
            continue
        cores_union = tuple(dict.fromkeys((*prev["cores"], *cores)))
        acrs_union = set(prev["acrs"]) | set(acrs)
        sector = prev["sector"] or fresh["sector"]
        prev.update(fresh)
        prev["cores"], prev["acrs"], prev["sector"] = cores_union, acrs_union, sector


@lru_cache(maxsize=2)
def _entries_cached(corte: Optional[str]) -> List[Dict]:
    """Padrón cacheado POR CORTE: la fecha ES la key.

    Antes el cache vivía todo el proceso, lo que con el loop diario significaba que el
    corte de mañana no se vería hasta reiniciar el server. `maxsize=2` porque durante el
    cambio de corte pueden convivir el viejo y el nuevo por un instante."""
    base = _csv_entries()
    if corte:
        _merge_corte(base)
    return base


def _entries() -> List[Dict]:
    return _entries_cached(_corte_vigente())


def invalidate_cache() -> None:
    """Tira los caches del padrón. La key por corte alcanza para el día a día; esto es
    para cuando cambia el CONTENIDO sin cambiar la fecha (re-siembra del store, tests).
    El loop diario la llama después de grabar: es gratis y evita razonar sobre el borde."""
    _entries_cached.cache_clear()
    _rating_for.cache_clear()


def _grade(rating: str) -> str:
    """Categoría de calidad para colorear el badge (de la letra base del rating)."""
    r = rating.upper().split("(")[0].strip()
    if r.startswith("AAA") or r.startswith("AA"):
        return "strong"
    if r.startswith("A"):
        return "good"
    if r.startswith("BBB"):
        return "mid"
    if r.startswith("BB") or r.startswith("B"):
        return "weak"
    return "distress"   # CCC/CC/C/D


def _containment_score(qc: FrozenSet[str], ec: FrozenSet[str]) -> Optional[Tuple[int, int]]:
    """(tokens compartidos, −tokens sobrantes) si un core contiene al otro; None si no.

    Ordena por especificidad: gana el candidato que comparte más tokens y, a igualdad,
    el que menos agrega — así "IRSA" prefiere "IRSA Inversiones y Representaciones"
    sobre cualquier otro emisor que también contenga el token."""
    if not (qc <= ec or ec <= qc):
        return None
    return len(qc & ec), -len(qc ^ ec)


def _match(q_cores: Tuple[FrozenSet[str], ...], q_acrs: Set[str], ents: List[Dict],
           *, allow_containment: bool = True) -> Optional[Dict]:
    """Entrada del padrón que corresponde al nombre ya normalizado, o None.

    Tres niveles de menor a mayor tolerancia. `allow_containment=False` corta después del
    acrónimo: lo usa el merge del corte, donde una colisión no deja al emisor sin rating
    sino que le pone el de otro."""
    # 1) core exacto (nombre pelado o alias entre paréntesis, de cualquiera de los lados)
    for e in ents:
        if any(qc == ec for qc in q_cores for ec in e["cores"]):
            return e
    # 2) acrónimo (paréntesis ∩ paréntesis, o token del core == acrónimo del otro)
    q_up = {t.upper() for qc in q_cores for t in qc}
    for e in ents:
        e_up = {t.upper() for ec in e["cores"] for t in ec}
        if (e["acrs"] & q_acrs) or (e["acrs"] & q_up) or (q_acrs & e_up):
            return e
    if not allow_containment:
        return None
    # 3) contención en CUALQUIER dirección (el catálogo trae el nombre comercial corto,
    #    el FIX el legal largo — y viceversa), con ganador único por especificidad.
    cands = []
    for e in ents:
        scores = [s for qc in q_cores for ec in e["cores"]
                  if (s := _containment_score(qc, ec)) is not None]
        if scores:
            cands.append((max(scores), e))
    if not cands:
        return None
    best = max(sc for sc, _ in cands)
    winners = [e for sc, e in cands if sc == best]
    # Un ÚNICO token compartido es evidencia débil: sólo vale si ningún otro emisor lo
    # comparte. Si no, "MOLINOS" elegiría al azar entre Molinos Agro y Molinos Río de la
    # Plata — mejor sin calificación que con la equivocada.
    weak = best[0] == 1 and sum(1 for sc, _ in cands if sc[0] == 1) > 1
    return winners[0] if len(winners) == 1 and not weak else None


def rating_for(emisor: Optional[str]) -> Optional[Dict[str, str]]:
    """Calificación del emisor, o None si no está en el listado FIX."""
    return _rating_for(emisor, _corte_vigente())


@lru_cache(maxsize=512)
def _rating_for(emisor: Optional[str], corte: Optional[str]) -> Optional[Dict[str, str]]:
    """Idéntico a `rating_for`, con el corte en la key para que el cache no sobreviva al
    cambio de padrón (el `corte` no se usa en el cuerpo: lo consume `_entries()`)."""
    if not emisor:
        return None
    q_cores, q_acrs = normalize_core(emisor)
    if not q_cores and not q_acrs:
        return None
    match = _match(q_cores, q_acrs, _entries())
    if match is None:
        return None
    return {"rating": match["rating"], "perspectiva": match["perspectiva"],
            "sector": match["sector"], "emisor": match["emisor"],
            "grade": _grade(match["rating"]), "as_of": match["as_of"], "source": SOURCE}


# Los consumidores (el loop de app.py, los tests) invalidan buscando `.cache_clear` en
# `rating_for`/`_entries` — que es lo que ambas exponían cuando el `lru_cache` vivía
# encima de ellas. El cache se mudó adentro (la key incluye el corte), así que se les deja
# el atributo apuntando al invalidador completo: nadie afuera tiene que saber dónde vive.
rating_for.cache_clear = invalidate_cache   # type: ignore[attr-defined]
_entries.cache_clear = invalidate_cache     # type: ignore[attr-defined]
