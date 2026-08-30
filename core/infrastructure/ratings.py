"""Calificaciones crediticias por emisor (FIX SCR, escala nacional argentina).

Data de referencia a nivel EMISOR (no por instrumento), en `data/calificaciones.csv`
(emisor, sector, rating, perspectiva). Se joinea al dataset de /on por nombre de emisor
con un matcher normalizado tolerante: ignora mayúsculas/acentos, sufijos societarios
(S.A./S.A.I.C./S.R.L.…), el sufijo "- Clase X" de las altas ABM, y matchea por
acrónimo entre paréntesis (EDENOR/EDESA/EDEMSA/CGC/PAME/PCR/SAMI/SAIEP/EPEC).

Cada nombre produce VARIOS cores: el del nombre "pelado" MÁS uno por cada grupo entre
paréntesis (así "YPF Energía Eléctrica S.A. (YPF Luz)" también se conoce como "YPF Luz",
y "Tango Energy … (Ex Petrolera Aconcagua Energía S.A.)" como "Petrolera Aconcagua
Energía"). El catálogo suele traer el nombre COMERCIAL corto ("IRSA") y el listado FIX el
legal largo ("IRSA Inversiones y Representaciones S.A."), así que la contención se evalúa
en AMBAS direcciones — pero exigiendo ganador ÚNICO por especificidad: ante empate no se
devuelve nada, porque un rating equivocado es peor que ningún rating.

`rating_for(emisor)` → {rating, perspectiva, sector, emisor, grade} o None.
Actualizá las calificaciones editando el CSV (la fecha del corte abajo en AS_OF).
"""
from __future__ import annotations

import csv
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

CSV_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "calificaciones.csv"
AS_OF = "2026-06-01"     # corte del listado FIX SCR
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


@lru_cache(maxsize=1)
def _entries() -> List[Dict]:
    out: List[Dict] = []
    if not CSV_PATH.exists():
        return out
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            cores, acrs = normalize_core(r["emisor"])
            out.append({"emisor": r["emisor"].strip(), "sector": r["sector"].strip(),
                        "rating": r["rating"].strip(), "perspectiva": r["perspectiva"].strip(),
                        "cores": cores, "acrs": acrs})
    return out


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


@lru_cache(maxsize=512)
def rating_for(emisor: Optional[str]) -> Optional[Dict[str, str]]:
    """Calificación del emisor, o None si no está en el listado FIX."""
    if not emisor:
        return None
    q_cores, q_acrs = normalize_core(emisor)
    if not q_cores and not q_acrs:
        return None
    ents = _entries()
    match = None
    # 1) core exacto (nombre pelado o alias entre paréntesis, de cualquiera de los lados)
    for e in ents:
        if any(qc == ec for qc in q_cores for ec in e["cores"]):
            match = e
            break
    # 2) acrónimo (paréntesis ∩ paréntesis, o token del core == acrónimo del otro)
    if match is None:
        q_up = {t.upper() for qc in q_cores for t in qc}
        for e in ents:
            e_up = {t.upper() for ec in e["cores"] for t in ec}
            if (e["acrs"] & q_acrs) or (e["acrs"] & q_up) or (q_acrs & e_up):
                match = e
                break
    # 3) contención en CUALQUIER dirección (el catálogo trae el nombre comercial corto,
    #    el FIX el legal largo — y viceversa), con ganador único por especificidad.
    if match is None:
        cands = []
        for e in ents:
            scores = [s for qc in q_cores for ec in e["cores"]
                      if (s := _containment_score(qc, ec)) is not None]
            if scores:
                cands.append((max(scores), e))
        if cands:
            best = max(sc for sc, _ in cands)
            winners = [e for sc, e in cands if sc == best]
            # Un ÚNICO token compartido es evidencia débil: sólo vale si ningún otro
            # emisor lo comparte. Si no, "MOLINOS" elegiría al azar entre Molinos Agro
            # y Molinos Río de la Plata — mejor sin calificación que con la equivocada.
            weak = best[0] == 1 and sum(1 for sc, _ in cands if sc[0] == 1) > 1
            if len(winners) == 1 and not weak:
                match = winners[0]
    if match is None:
        return None
    return {"rating": match["rating"], "perspectiva": match["perspectiva"],
            "sector": match["sector"], "emisor": match["emisor"],
            "grade": _grade(match["rating"]), "as_of": AS_OF, "source": SOURCE}
