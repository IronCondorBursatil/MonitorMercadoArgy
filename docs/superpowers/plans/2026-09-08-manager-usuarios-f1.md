# Manager de usuarios v2 — Fase 1 · Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Manager `/users` con la Opción A (tabla + ficha lateral HTMX), columnas nuevas en `users`, cuenta deshabilitable que bloquea login y sesiones, cierre de sesiones, último ingreso, y login con el look del resto de la app. Sin tokens ni mail (Fases 2 y 3).

**Architecture:** Las columnas entran por la migración forward-only de `init_db` (ALTER ADD COLUMN). Las reglas puras (email, estado, actividad derivada, formato de fechas) viven en un módulo nuevo `apps/web/users_service.py` testeable sin app. El router `users_abm.py` se reescribe con una ruta POST por acción y un fragmento HTMX para la ficha; toda respuesta HTML pasa por `_users_page`, que arma el contexto completo (filas, resumen, ficha seleccionada). El login y las páginas públicas futuras extienden un `base_public.html` con el header de `base.html` sin nav.

**Tech Stack:** FastAPI 0.141 + Jinja2 + HTMX 2.0.3 (vendored) · SQLAlchemy 2 sobre SQLite (`catalog.db`) · passlib/bcrypt · pytest con `TestClient`.

**Spec:** `docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md` (§0, §1, §2.1, §2.3, §3.1, §5.1, §5.3, §6, §8 y la fila F1 de §9).

## Global Constraints

- Intérprete: siempre `py -3.12` (nunca `python`/`pytest` pelados). Gate: `pwsh scripts/check.ps1` (ruff + pytest completo, ~2:45).
- Schema **forward-only**: sólo `ALTER ADD COLUMN` vía `_migrate_table_add_columns`; NO sube `CURRENT_SCHEMA_VERSION`; nunca `drop`.
- Nada de `.db` dentro del árbol; los tests usan la DB de test que fija `conftest.py` (`MONITOR_DB_DIR`).
- Tests de auth REAL llevan `@pytest.mark.noauth` (la fixture autouse `_auth_bypass` corre todo lo demás como admin falso `id=1`, `username="test-admin"`).
- Fechas: `datetime.now()` naive en hora del proceso (ART por `apply_timezone`).
- Política de contraseña: mínimo 10 caracteres, máximo 72 bytes (bcrypt trunca en silencio).
- Auditoría: logger `monitor.audit`, campos saneados con `_limpio`, `extra={"console": True}`. Nunca la contraseña.
- Username: la validación existente (`_username_invalido`) y el patrón anti-XSS de `users.html` (el username va en `data-*`, nunca dentro de un handler `on*`) se conservan; lo fija `test_aud_D1_seguridad_web.py::test_users_page_no_mete_el_username_en_atributos_de_evento`.
- Rutas públicas: sin cambios en esta fase (`_PUBLIC_PATHS` = `/login`, `/logout`, `/api/health`).
- Commits: uno por tarea, en la rama `feat/manager-usuarios-f1` de un worktree FUERA del proyecto; mensajes en castellano, imperativo, con `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Pedir OK a David antes del primer commit de la sesión (agents.md §0.1.6). Nunca push ni deploy sin OK.
- `/security-review` antes de pushear (toca `deps_auth.py`, `routers/auth.py`, `routers/users_abm.py`).

---

### Task 0: Rama, worktree y baseline

**Files:**
- Copy: `docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md` (hoy está sin commitear en el árbol principal) y `docs/superpowers/plans/2026-09-08-manager-usuarios-f1.md`

- [ ] **Step 1: Crear el worktree** con la skill `superpowers:using-git-worktrees` (`EnterWorktree`), rama `feat/manager-usuarios-f1` desde `main`. El worktree va fuera del proyecto (`~/.config/superpowers/worktrees/` o el que elija la skill), nunca dentro de `Monitores - Data912`.

- [ ] **Step 2: Copiar la spec y este plan al worktree** (en `main` no están commiteados). Desde PowerShell, con `$W` = ruta del worktree:

```powershell
Copy-Item "docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md" "$W/docs/superpowers/specs/"
Copy-Item "docs/superpowers/plans/2026-09-08-manager-usuarios-f1.md" "$W/docs/superpowers/plans/"
```

- [ ] **Step 3: Baseline del gate** en el worktree: `pwsh scripts/check.ps1 -Fast`. Esperado: verde (o anotar los fallos preexistentes ANTES de tocar nada; agents.md §0.1.7).

- [ ] **Step 4: Commit de spec + plan**

```bash
git add docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md docs/superpowers/plans/2026-09-08-manager-usuarios-f1.md
git commit -m "Manager v2: spec de diseño y plan de la Fase 1"
```

---

### Task 1: Columnas nuevas en `users` + índice único parcial de email

**Files:**
- Modify: `core/infrastructure/db/models.py:24-38` (clase `UserORM`)
- Modify: `core/infrastructure/db/catalog_repository.py:315-323` (tupla de DDL de índices en `init_db`)
- Test: `tests/test_users_manager.py` (nuevo)

**Interfaces:**
- Produces: `UserORM.email: str|None`, `full_name: str|None`, `notes: str|None`, `is_active: bool` (NOT NULL, default True), `created_at: datetime|None`, `created_by: str|None`, `last_login_at: datetime|None`, `last_login_ip: str|None`, `password_changed_at: datetime|None`. Índice `ux_users_email` único sólo cuando `email IS NOT NULL`.

- [ ] **Step 1: Escribir los tests que fallan**

Crear `tests/test_users_manager.py`:

```python
"""Manager de usuarios v2 (Fase 1): schema, reglas puras, is_active, rutas del admin.
Spec: docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md."""

import sqlalchemy as sa

from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import UserORM


def test_columnas_nuevas_entran_por_migracion_forward_only(tmp_path):
    """Sobre una tabla `users` PREEXISTENTE (schema viejo, con una fila), `init_db`
    agrega las columnas con ALTER, la fila sobrevive y queda ACTIVA (default 1)."""
    from config.settings import settings as _s
    from core.infrastructure.db import engine as db_engine
    from core.infrastructure.db.catalog_repository import init_db

    db = tmp_path / "vieja.db"
    eng = sa.create_engine(f"sqlite:///{db}")
    with eng.begin() as con:
        con.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "username VARCHAR NOT NULL, hashed_password VARCHAR NOT NULL, "
            "is_admin BOOLEAN, allowed_tabs JSON, token_version INTEGER)")
        con.exec_driver_sql(
            "INSERT INTO users (username, hashed_password, is_admin, allowed_tabs, token_version) "
            "VALUES ('viejo', 'x', 0, '[]', 0)")
    eng.dispose()

    db_engine.configure(db)
    try:
        init_db()
        with SessionLocal() as s:
            fila = s.query(UserORM).filter(UserORM.username == "viejo").first()
            assert fila is not None, "la fila vieja no sobrevivió"
            assert fila.is_active is True, "una fila vieja tiene que quedar ACTIVA"
            assert fila.email is None and fila.full_name is None
            assert fila.last_login_at is None and fila.created_at is None
    finally:
        db_engine.configure(_s.catalog_db)


def test_email_es_unico_solo_cuando_no_es_nulo(tmp_path):
    """Dos usuarios SIN email conviven; dos con el MISMO email no (índice parcial)."""
    from config.settings import settings as _s
    from core.infrastructure.db import engine as db_engine
    from core.infrastructure.db.catalog_repository import init_db

    db_engine.configure(tmp_path / "nueva.db")
    try:
        init_db()
        with SessionLocal() as s:
            s.add(UserORM(username="a", hashed_password="x", is_admin=False, allowed_tabs=[]))
            s.add(UserORM(username="b", hashed_password="x", is_admin=False, allowed_tabs=[]))
            s.commit()                       # dos NULL: OK
            s.add(UserORM(username="c", hashed_password="x", is_admin=False, allowed_tabs=[],
                          email="dup@ejemplo.com"))
            s.commit()
            s.add(UserORM(username="d", hashed_password="x", is_admin=False, allowed_tabs=[],
                          email="dup@ejemplo.com"))
            try:
                s.commit()
                raise AssertionError("dos usuarios con el mismo email pasaron")
            except sa.exc.IntegrityError:
                s.rollback()
    finally:
        db_engine.configure(_s.catalog_db)
```

- [ ] **Step 2: Correr y ver que fallan**

Run: `py -3.12 -m pytest tests/test_users_manager.py -q`
Expected: 2 FAIL (`AttributeError: ... is_active` / `email` no es columna).

- [ ] **Step 3: Agregar las columnas al ORM**

En `core/infrastructure/db/models.py`, cambiar el import de fechas y la clase:

```python
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import DateTime, ForeignKey, Index, String
```

y dentro de `UserORM`, después de `token_version`:

```python
    # --- Manager v2 (spec 2026-09-08-manager-usuarios-reseteo) ---------------
    # Todas nullable salvo is_active: entran por ALTER ADD COLUMN en init_db
    # (forward-only) y las filas viejas quedan en NULL / activas.
    email: Mapped[Optional[str]] = mapped_column(String, default=None)   # minúsculas; único si no es NULL (índice parcial en init_db)
    full_name: Mapped[Optional[str]] = mapped_column(String, default=None)
    notes: Mapped[Optional[str]] = mapped_column(String, default=None)
    # 0 = deshabilitado: no puede loguearse y sus sesiones vivas mueren en el
    # próximo request (deps_auth). Distinto de borrar: los datos quedan.
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    created_by: Mapped[Optional[str]] = mapped_column(String, default=None)   # username del admin
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    last_login_ip: Mapped[Optional[str]] = mapped_column(String, default=None)
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
```

- [ ] **Step 4: Agregar el índice parcial en `init_db`**

En `core/infrastructure/db/catalog_repository.py`, la tupla de DDL dentro de `init_db` pasa a:

```python
            for ddl in (
                "CREATE INDEX IF NOT EXISTS ix_instr_mep ON instruments (ticker_mep)",
                "CREATE INDEX IF NOT EXISTS ix_instr_ccl ON instruments (ticker_ccl)",
                "CREATE INDEX IF NOT EXISTS ix_instr_isin ON instruments (isin)",
                # Email único SOLO cuando hay email: los usuarios sin email (los de
                # antes del Manager v2, o los que se cargan a mano) conviven.
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email ON users (email) "
                "WHERE email IS NOT NULL",
            ):
```

- [ ] **Step 5: Correr los tests**

Run: `py -3.12 -m pytest tests/test_users_manager.py tests/test_sec_audit_y_passwords.py::test_la_columna_entra_por_migracion_forward_only -q`
Expected: 3 PASS. (El ALTER de `is_active` sale como `... BOOLEAN DEFAULT True NOT NULL`; SQLite ≥ 3.23 acepta `True` como literal. Si fallara, mirar el log `catalog: ALTER users ADD COLUMN`.)

- [ ] **Step 6: Commit**

```bash
git add core/infrastructure/db/models.py core/infrastructure/db/catalog_repository.py tests/test_users_manager.py
git commit -m "Manager v2: columnas de perfil, estado y actividad en users (forward-only) + email único parcial"
```

---

### Task 2: Política de contraseña única en `core/security.py`

**Files:**
- Modify: `core/security.py`
- Modify: `apps/web/routers/users_abm.py:57-73` (borrar `_PASSWORD_MIN`, `_PASSWORD_MAX_BYTES`, `_password_invalida`; importar la nueva)
- Test: `tests/test_users_manager.py`

**Interfaces:**
- Produces: `core.security.password_invalida(pw: str) -> str | None` (motivo o `None` si sirve). Misma semántica que la función que hoy vive en `users_abm.py`.

- [ ] **Step 1: Test**

Agregar a `tests/test_users_manager.py`:

```python
def test_password_invalida_es_la_politica_unica():
    from core.security import password_invalida

    assert password_invalida("corta") is not None
    assert password_invalida("a" * 9) is not None
    assert password_invalida("a" * 10) is None
    assert password_invalida("á" * 40) is not None      # 80 bytes > 72: bcrypt truncaría
    assert password_invalida("a" * 72) is None
```

- [ ] **Step 2: Correr y ver que falla**

Run: `py -3.12 -m pytest tests/test_users_manager.py::test_password_invalida_es_la_politica_unica -q`
Expected: FAIL `ImportError: cannot import name 'password_invalida'`.

- [ ] **Step 3: Mover la función**

En `core/security.py`, al final:

```python
# Política de contraseña ÚNICA (la usan la ABM de usuarios y, desde la Fase 2, la
# página pública /reset/{token}). El máximo es el límite REAL de bcrypt: pasados
# 72 bytes trunca EN SILENCIO (passlib con truncate_error=False), o sea que
# "misuperclave...<80 chars>" y sus primeros 72 bytes serían la misma contraseña.
PASSWORD_MIN = 10
PASSWORD_MAX_BYTES = 72


def password_invalida(pw: str) -> Optional[str]:
    """Motivo por el que `pw` no sirve, o None si está bien."""
    if len(pw or "") < PASSWORD_MIN:
        return f"La contraseña tiene que tener al menos {PASSWORD_MIN} caracteres."
    if len((pw or "").encode("utf-8")) > PASSWORD_MAX_BYTES:
        return ("La contraseña supera los 72 bytes: bcrypt trunca en silencio a partir "
                "de ahí, así que el resto no protegería nada.")
    return None
```

En `apps/web/routers/users_abm.py`: borrar el bloque `_PASSWORD_MIN` / `_PASSWORD_MAX_BYTES` / `_password_invalida` (líneas 57-73), cambiar el import a `from core.security import get_password_hash, password_invalida` y reemplazar las dos llamadas `_password_invalida(` por `password_invalida(`.

- [ ] **Step 4: Correr**

Run: `py -3.12 -m pytest tests/test_users_manager.py tests/test_sec_audit_y_passwords.py -q`
Expected: todo PASS (los tests de longitud de contraseña de la ABM siguen verdes).

- [ ] **Step 5: Commit**

```bash
git add core/security.py apps/web/routers/users_abm.py tests/test_users_manager.py
git commit -m "Política de contraseña única en core/security.password_invalida"
```

---

### Task 3: Reglas puras del Manager — `apps/web/users_service.py`

**Files:**
- Create: `apps/web/users_service.py`
- Test: `tests/test_users_manager.py`

**Interfaces:**
- Produces:
  - `TABS: tuple[tuple[str, str], ...]` (clave, etiqueta) en el orden del nav; `TAB_KEYS: frozenset[str]`.
  - `normalizar_email(raw) -> str | None` ('' → None; minúsculas; trim).
  - `email_invalido(email: str | None) -> str` (motivo o '' si sirve; `None` es válido = sin email).
  - `iniciales(u) -> str`, `fmt_momento(dt, hoy=None) -> str`, `estado_usuario(u) -> str` (`"activo"|"deshabilitado"`).
  - `vista_usuario(u, hoy=None) -> dict` con claves `u, iniciales, estado, sin_email, ultimo_acceso, pwd_cambiada, alta, tabs`.
  - `actividad_reciente(u, hoy=None) -> list[dict]` (claves `cuando, que, detalle, texto`, del más nuevo al más viejo).
  - `resumen(users) -> dict` con `total, activos, deshabilitados, sin_email`.

- [ ] **Step 1: Tests**

Agregar a `tests/test_users_manager.py`:

```python
from datetime import date, datetime


def _u(**kw):
    base = dict(username="mcaceres", hashed_password="x", is_admin=False,
                allowed_tabs=["bonos", "on"], is_active=True)
    base.update(kw)
    return UserORM(**base)


def test_normalizar_email():
    from apps.web.users_service import normalizar_email
    assert normalizar_email("  M.Caceres@Ejemplo.com ") == "m.caceres@ejemplo.com"
    assert normalizar_email("") is None
    assert normalizar_email(None) is None


def test_email_invalido():
    from apps.web.users_service import email_invalido
    assert email_invalido(None) == ""                       # sin email es válido
    assert email_invalido("m.caceres@ejemplo.com") == ""
    assert email_invalido("sin-arroba") != ""
    assert email_invalido("a@b") != ""                      # dominio sin punto
    assert email_invalido("a@.com") != ""
    assert email_invalido('x"onmouseover=1@ejemplo.com') != ""   # mismos prohibidos que el username
    assert email_invalido("a" * 250 + "@x.co") != ""        # > 254


def test_fmt_momento():
    from apps.web.users_service import fmt_momento
    hoy = date(2026, 9, 8)
    assert fmt_momento(None, hoy) == "—"
    assert fmt_momento(datetime(2026, 9, 8, 9, 12), hoy) == "hoy 09:12"
    assert fmt_momento(datetime(2026, 9, 7, 18, 40), hoy) == "ayer 18:40"
    assert fmt_momento(datetime(2026, 8, 12, 10, 0), hoy) == "12 ago"
    assert fmt_momento(datetime(2025, 9, 3, 10, 0), hoy) == "03 sep 2025"


def test_vista_usuario_y_estado():
    from apps.web.users_service import estado_usuario, iniciales, vista_usuario
    u = _u(full_name="Mariana Cáceres", email=None, last_login_at=datetime(2026, 9, 8, 9, 12))
    assert iniciales(u) == "MC"
    assert iniciales(_u(full_name=None)) == "M"
    assert estado_usuario(u) == "activo"
    assert estado_usuario(_u(is_active=False)) == "deshabilitado"
    v = vista_usuario(u, hoy=date(2026, 9, 8))
    assert v["sin_email"] is True and v["ultimo_acceso"] == "hoy 09:12"
    assert v["tabs"] == ["bonos", "on"]
    assert vista_usuario(_u(is_admin=True, allowed_tabs=["*"]))["tabs"] == []


def test_actividad_reciente_derivada_y_ordenada():
    from apps.web.users_service import actividad_reciente
    u = _u(created_at=datetime(2026, 7, 15, 11, 20), created_by="admin",
           password_changed_at=datetime(2026, 8, 21, 10, 3),
           last_login_at=datetime(2026, 9, 8, 9, 12), last_login_ip="181.1.2.3")
    ev = actividad_reciente(u, hoy=date(2026, 9, 8))
    assert [e["que"] for e in ev] == ["Ingreso", "Contraseña cambiada", "Alta"]
    assert ev[0]["texto"] == "hoy 09:12" and ev[0]["detalle"] == "181.1.2.3"
    assert ev[2]["detalle"] == "por admin"
    assert actividad_reciente(_u()) == []


def test_resumen():
    from apps.web.users_service import resumen
    r = resumen([_u(), _u(is_active=False), _u(email="a@b.co")])
    assert r == {"total": 3, "activos": 2, "deshabilitados": 1, "sin_email": 2}
```

- [ ] **Step 2: Correr y ver que fallan**

Run: `py -3.12 -m pytest tests/test_users_manager.py -q -k "email or momento or vista or actividad or resumen"`
Expected: 6 FAIL con `ModuleNotFoundError: apps.web.users_service`.

- [ ] **Step 3: Implementar**

Crear `apps/web/users_service.py`:

```python
"""Reglas PURAS del Manager de usuarios (sin FastAPI ni sesión de DB): normalización y
validación de email, estado derivado, actividad reciente y formato de fechas. Las
prueba `tests/test_users_manager.py` sin levantar la app. El router
(`routers/users_abm.py`) sólo orquesta.

Spec: docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md §2.3, §6."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from core.infrastructure.db.models import UserORM

# Pestañas en el MISMO orden que el nav de base.html y que `_TAB_LANDING` (auth.py).
TABS: tuple[tuple[str, str], ...] = (
    ("bonos", "Bonos"), ("on", "O.N's"), ("curva", "Curva"), ("cartera", "Cartera"),
    ("bcra", "BCRA"), ("cashflows", "Cashflows"), ("fci", "FCI"),
    ("escenarios", "Escenarios"), ("opciones", "Opciones"), ("catalogo", "Catálogo"),
    ("abm", "ABM Bonos"),
)
TAB_KEYS = frozenset(k for k, _ in TABS)

_EMAIL_MAX = 254
# Los mismos caracteres que rechaza el username (defensa en profundidad contra XSS
# almacenado: el email se re-renderiza en /users) + espacio y coma, que un email
# válido nunca lleva.
_EMAIL_PROHIBIDO = frozenset(["<", ">", '"', "'", "&", "`", "\\", " ", ","])
_MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def normalizar_email(raw) -> Optional[str]:
    """'' / None → None (sin email); el resto en minúsculas y sin espacios en los bordes."""
    s = (raw or "").strip().lower()
    return s or None


def email_invalido(email: Optional[str]) -> str:
    """Motivo por el que `email` no sirve, o '' si está bien. `None` = sin email = válido."""
    if email is None:
        return ""
    if len(email) > _EMAIL_MAX:
        return f"El email no puede superar {_EMAIL_MAX} caracteres."
    if any(c in _EMAIL_PROHIBIDO or ord(c) < 32 for c in email):
        return "El email tiene caracteres no permitidos."
    local, sep, dominio = email.partition("@")
    if (not sep or not local or "@" in dominio or "." not in dominio
            or dominio.startswith(".") or dominio.endswith(".")):
        return "El email no tiene un formato válido (nombre@dominio)."
    return ""


def iniciales(u: UserORM) -> str:
    partes = (u.full_name or u.username or "?").split()
    return "".join(p[0] for p in partes[:2]).upper()


def fmt_momento(dt: Optional[datetime], hoy: Optional[date] = None) -> str:
    """'hoy 09:12' · 'ayer 18:40' · '12 ago' · '03 sep 2025' (otro año) · '—' sin dato."""
    if dt is None:
        return "—"
    hoy = hoy or date.today()
    d = dt.date()
    if d == hoy:
        return f"hoy {dt:%H:%M}"
    if (hoy - d).days == 1:
        return f"ayer {dt:%H:%M}"
    if d.year == hoy.year:
        return f"{d.day:02d} {_MESES[d.month - 1]}"
    return f"{d.day:02d} {_MESES[d.month - 1]} {d.year}"


def estado_usuario(u: UserORM) -> str:
    return "activo" if u.is_active else "deshabilitado"


def vista_usuario(u: UserORM, hoy: Optional[date] = None) -> dict:
    """Lo que la tabla y la cabecera de la ficha muestran de un usuario."""
    return {
        "u": u,
        "iniciales": iniciales(u),
        "estado": estado_usuario(u),
        "sin_email": not u.email,
        "ultimo_acceso": fmt_momento(u.last_login_at, hoy),
        "pwd_cambiada": fmt_momento(u.password_changed_at, hoy),
        "alta": fmt_momento(u.created_at, hoy),
        "tabs": [] if u.is_admin else [t for t in (u.allowed_tabs or []) if t != "*"],
    }


def actividad_reciente(u: UserORM, hoy: Optional[date] = None) -> list[dict]:
    """Eventos DERIVADOS de las columnas (sin tabla de eventos), del más nuevo al más viejo.
    La Fase 2 suma los tokens (link emitido / consumido / invitación aceptada)."""
    ev: list[dict] = []
    if u.last_login_at:
        ev.append({"cuando": u.last_login_at, "que": "Ingreso", "detalle": u.last_login_ip or ""})
    if u.password_changed_at:
        ev.append({"cuando": u.password_changed_at, "que": "Contraseña cambiada",
                   "detalle": "por un administrador"})
    if u.created_at:
        ev.append({"cuando": u.created_at, "que": "Alta",
                   "detalle": f"por {u.created_by}" if u.created_by else ""})
    ev.sort(key=lambda e: e["cuando"], reverse=True)
    for e in ev:
        e["texto"] = fmt_momento(e["cuando"], hoy)
    return ev


def resumen(users) -> dict:
    return {
        "total": len(users),
        "activos": sum(1 for u in users if u.is_active),
        "deshabilitados": sum(1 for u in users if not u.is_active),
        "sin_email": sum(1 for u in users if not u.email),
    }
```

- [ ] **Step 4: Correr**

Run: `py -3.12 -m pytest tests/test_users_manager.py -q`
Expected: todo PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/web/users_service.py tests/test_users_manager.py
git commit -m "Manager v2: reglas puras (email, estado, actividad derivada, formato de fechas)"
```

---

### Task 4: `is_active` en login y sesión + último ingreso

**Files:**
- Modify: `apps/web/deps_auth.py:88-108` (`_get_user_from_token`)
- Modify: `apps/web/routers/auth.py:148-171` (`login`)
- Test: `tests/test_users_manager.py`

**Interfaces:**
- Consumes: `UserORM.is_active`, `last_login_at`, `last_login_ip` (Task 1).
- Produces: un usuario con `is_active=False` no entra (mismo mensaje que credenciales inválidas) y su cookie vigente deja de valer; el login exitoso escribe `last_login_at` y `last_login_ip`.

- [ ] **Step 1: Tests (auth real)**

Agregar a `tests/test_users_manager.py` (arriba, junto a los imports, `import pytest`, `from fastapi.testclient import TestClient`, `from apps.web.app import app`, `from apps.web.routers import auth as auth_router`, `from core.infrastructure.db.engine import get_engine`, `from core.infrastructure.db.models import Base`, `from core.security import get_password_hash`):

```python
@pytest.fixture
def usuarios():
    """admin + bob (sólo 'bonos') en la DB de test; limiter del login limpio."""
    Base.metadata.create_all(bind=get_engine())
    auth_router._login_attempts.clear()
    with SessionLocal() as s:
        s.query(UserORM).delete()
        s.add(UserORM(username="admin", hashed_password=get_password_hash("adminpass1"),
                      is_admin=True, allowed_tabs=["*"], is_active=True))
        s.add(UserORM(username="bob", hashed_password=get_password_hash("bobpass1234"),
                      is_admin=False, allowed_tabs=["bonos"], is_active=True,
                      email="bob@ejemplo.com", full_name="Bob Pérez"))
        s.commit()
    yield
    with SessionLocal() as s:
        s.query(UserORM).delete()
        s.commit()
    auth_router._login_attempts.clear()


def _login(c, user, pw):
    return c.post("/login", data={"username": user, "password": pw}, follow_redirects=False)


def _login_admin(c):
    r = _login(c, "admin", "adminpass1")
    assert r.status_code in (302, 303)


def _bob_id():
    with SessionLocal() as s:
        return s.query(UserORM).filter(UserORM.username == "bob").first().id


def _set_bob(**kw):
    with SessionLocal() as s:
        bob = s.query(UserORM).filter(UserORM.username == "bob").first()
        for k, v in kw.items():
            setattr(bob, k, v)
        s.commit()


# ── is_active ───────────────────────────────────────────────────────────────
@pytest.mark.noauth
def test_usuario_deshabilitado_no_entra_y_recibe_el_mismo_mensaje(usuarios):
    """Mutación: si se saca el chequeo de is_active en routers/auth.login, esto da 302."""
    _set_bob(is_active=False)
    with TestClient(app) as c:
        r_off = _login(c, "bob", "bobpass1234")
        r_mal = _login(c, "bob", "clave-incorrecta")
    assert r_off.status_code == 200 and "access_token" not in r_off.cookies
    assert "Usuario o contraseña incorrectos" in r_off.text
    assert r_off.text == r_mal.text, "deshabilitado y clave incorrecta tienen que verse IGUAL"


@pytest.mark.noauth
def test_deshabilitar_mata_la_sesion_viva(usuarios):
    """La cookie sigue siendo válida (misma token_version) pero deps_auth rechaza al
    inactivo. Mutación: sacar el chequeo en `_get_user_from_token` → 200."""
    with TestClient(app) as c:
        assert _login(c, "bob", "bobpass1234").status_code in (302, 303)
        assert c.get("/", follow_redirects=False).status_code == 200
        _set_bob(is_active=False)
        assert c.get("/", follow_redirects=False).status_code == 302


@pytest.mark.noauth
def test_login_exitoso_registra_ultimo_ingreso(usuarios):
    with TestClient(app) as c:
        assert _login(c, "bob", "bobpass1234").status_code in (302, 303)
    with SessionLocal() as s:
        bob = s.query(UserORM).filter(UserORM.username == "bob").first()
        assert bob.last_login_at is not None
        assert (datetime.now() - bob.last_login_at).total_seconds() < 60
        assert bob.last_login_ip                       # 'testclient' en TestClient
```

- [ ] **Step 2: Correr y ver que fallan**

Run: `py -3.12 -m pytest tests/test_users_manager.py -q -k "deshabilitado or ultimo_ingreso"`
Expected: 3 FAIL (el deshabilitado entra; `last_login_at` queda None).

- [ ] **Step 3: `deps_auth._get_user_from_token`**

Después del chequeo de `ver` (línea ~107), antes del `return _publish(request, user)`:

```python
    # Cuenta deshabilitada desde el Manager: la cookie puede ser válida (misma
    # token_version) pero el usuario ya no tiene acceso. Se corta acá para que
    # la sesión viva muera en el request siguiente, sin esperar a que expire.
    if not user.is_active:
        return _publish(request, None)
    return _publish(request, user)
```

- [ ] **Step 4: `routers/auth.login`**

Agregar `from datetime import datetime` arriba. Reemplazar el bloque del fallo y el éxito:

```python
    user = db.query(UserORM).filter(UserORM.username == username).first()
    # Verificar SIEMPRE un hash (contra el real o el dummy) → mismo tiempo con/sin usuario.
    ok = verify_password(password, user.hashed_password) if user else verify_password(password, _dummy_hash())
    # Un usuario DESHABILITADO recibe exactamente la misma respuesta que una clave
    # incorrecta: no se le confirma que la cuenta existe. El motivo va sólo al log.
    if not user or not ok or not user.is_active:
        _login_attempts[key].append(now)
        _audit.info("auth login=fail user=%s ip=%s%s", _limpio(username), _limpio(key[0]),
                    " motivo=deshabilitado" if (user and ok) else "", extra={"console": True})
        return _TEMPLATES.TemplateResponse(request, "pages/login.html", {"error": "Usuario o contraseña incorrectos"})

    _login_attempts.pop(key, None)   # login OK → limpiar el contador
    user.last_login_at = datetime.now()
    user.last_login_ip = key[0][:64]
    db.commit()
    _audit.info("auth login=ok user=%s ip=%s", _limpio(user.username), _limpio(key[0]),
                extra={"console": True})
```

- [ ] **Step 5: Correr**

Run: `py -3.12 -m pytest tests/test_users_manager.py tests/test_auth.py tests/test_sec_audit_y_passwords.py -q`
Expected: todo PASS.

- [ ] **Step 6: Prueba por mutación** (agents.md §0.1.8): comentar la línea `if not user.is_active:` de `deps_auth.py` → `test_deshabilitar_mata_la_sesion_viva` debe ponerse ROJO; restaurar. Quitar `or not user.is_active` del login → `test_usuario_deshabilitado_no_entra...` ROJO; restaurar.

- [ ] **Step 7: Commit**

```bash
git add apps/web/deps_auth.py apps/web/routers/auth.py tests/test_users_manager.py
git commit -m "Auth: la cuenta deshabilitada no entra ni conserva sesión; el login registra último ingreso"
```

---

### Task 5: Templates de la Opción A + `GET /users` (`?u=`) + `GET /users/{id}/ficha`

**Files:**
- Modify: `apps/web/routers/users_abm.py` (helpers `_users_page`/`_ctx_ficha`, `list_users`, `ficha`; todos los handlers pasan a responder con `_users_page`)
- Rewrite: `apps/web/templates/pages/users.html`
- Create: `apps/web/templates/fragments/user_ficha.html`
- Test: `tests/test_users_manager.py`

**Interfaces:**
- Consumes: `users_service.vista_usuario`, `actividad_reciente`, `resumen`, `TABS` (Task 3).
- Produces: `_users_page(request, db, *, status_code=200, selected_id=None, **ctx)` → contexto `users, filas, resumen, TABS, selected` (+ `u, v, actividad` si hay seleccionado); `_ctx_ficha(u) -> dict(u, v, actividad, TABS)`; ruta `GET /users?u=<id>`; ruta `GET /users/{user_id}/ficha` (fragmento; 404 si no existe). Las rutas POST que las plantillas invocan (`/users/{id}/datos`, `/permisos`, `/reset`, `/sesiones/cerrar`, `/estado`) se implementan en las Tasks 6-8; en esta task las plantillas ya las referencian.

- [ ] **Step 1: Tests**

Agregar a `tests/test_users_manager.py`:

```python
# ── GET /users y ficha ──────────────────────────────────────────────────────
@pytest.mark.noauth
def test_la_tabla_muestra_email_estado_y_link_a_la_ficha(usuarios):
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        r = c.get("/users")
    assert r.status_code == 200
    assert "bob@ejemplo.com" in r.text and "Bob Pérez" in r.text
    assert f'hx-get="/users/{bob}/ficha"' in r.text
    assert "Elegí un usuario" in r.text            # sin selección: panel vacío


@pytest.mark.noauth
def test_u_en_la_query_precarga_la_ficha(usuarios):
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        r = c.get(f"/users?u={bob}")
    assert r.status_code == 200
    assert f'action="/users/{bob}/datos"' in r.text
    assert f'action="/users/{bob}/permisos"' in r.text
    assert "Elegí un usuario" not in r.text


@pytest.mark.noauth
def test_la_ficha_es_un_fragmento(usuarios):
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        r = c.get(f"/users/{bob}/ficha")
        r404 = c.get("/users/999999/ficha")
    assert r.status_code == 200
    assert "<html" not in r.text.lower() and "Permisos" in r.text
    assert f'action="/users/{bob}/sesiones/cerrar"' in r.text
    assert f'action="/users/{bob}/estado"' in r.text
    assert r404.status_code == 404
```

- [ ] **Step 2: Correr y ver que fallan**

Run: `py -3.12 -m pytest tests/test_users_manager.py -q -k "tabla or precarga or fragmento"`
Expected: 3 FAIL.

- [ ] **Step 3: Helpers y rutas GET en `users_abm.py`**

Reemplazar la cabecera del módulo (imports, `_users_page`, `_no_existe`, `list_users`) por:

```python
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from apps.web.deps_auth import get_db, get_admin_user_html
from apps.web.templates import TEMPLATES as _TEMPLATES
from apps.web.users_service import (TABS, TAB_KEYS, actividad_reciente, email_invalido,
                                    normalizar_email, resumen, vista_usuario)
from core.infrastructure.db.models import UserORM
from core.security import get_password_hash, password_invalida

router = APIRouter(dependencies=[Depends(get_admin_user_html)])

# Auditoría de las acciones de admin: quién le hizo qué a quién (journal vía el filtro
# de consola de settings, que deja pasar INFO sólo con `console=True`).
_audit = logging.getLogger("monitor.audit")

# Caracteres que no pueden aparecer en un nombre de usuario legítimo y sí son el
# vector de un XSS almacenado (el username se re-renderiza en /users). Defensa en
# profundidad: el escape correcto vive en la plantilla, esto evita que el payload
# siquiera se persista. Se rechaza también cualquier carácter de control.
_USERNAME_PROHIBIDO = frozenset(["<", ">", chr(34), chr(39), "&", "`", chr(92)])
_USERNAME_MAX = 64
_NOMBRE_MAX = 120
_NOTAS_MAX = 500


def _limpio(v) -> str:
    return "".join(ch for ch in str(v) if ch.isprintable())[:64]


def _username_invalido(username: str) -> str:
    if not username or not username.strip():
        return "El nombre de usuario no puede estar vacío."
    if len(username) > _USERNAME_MAX:
        return f"El nombre de usuario no puede superar {_USERNAME_MAX} caracteres."
    if any(c in _USERNAME_PROHIBIDO or ord(c) < 32 for c in username):
        return "El nombre de usuario tiene caracteres no permitidos."
    return ""


def _texto(v, maximo: int) -> Optional[str]:
    """Campo de texto libre del form: recortado, vacío → None, acotado a `maximo`."""
    s = (v or "").strip()
    return s[:maximo] or None


def _tabs_validas(tabs) -> List[str]:
    return [t for t in (tabs or []) if t in TAB_KEYS]


def _ctx_ficha(u: UserORM) -> dict:
    return {"u": u, "v": vista_usuario(u), "actividad": actividad_reciente(u), "TABS": TABS}


def _users_page(request, db, *, status_code: int = 200, selected_id: Optional[int] = None, **ctx):
    """ÚNICA forma de responder la página: arma todo el contexto (filas, resumen, ficha
    seleccionada). `selected_id` mantiene la ficha abierta tras un POST."""
    users = db.query(UserORM).order_by(UserORM.username).all()
    selected = db.get(UserORM, selected_id) if selected_id is not None else None
    context = {"users": users, "filas": [vista_usuario(u) for u in users],
               "resumen": resumen(users), "TABS": TABS, "selected": selected, **ctx}
    if selected is not None:
        context.update(_ctx_ficha(selected))
    return _TEMPLATES.TemplateResponse(request, "pages/users.html", context,
                                       status_code=status_code)


def _no_existe(request, db, user_id: int):
    """404 explícito: antes se interpolaba `user.username` con `user is None` → 500."""
    return _users_page(request, db, status_code=404,
                       error=f"No existe el usuario id={user_id}.")


@router.get("/users", response_class=HTMLResponse)
def list_users(request: Request, u: Optional[int] = None, db: Session = Depends(get_db)):
    return _users_page(request, db, selected_id=u)


@router.get("/users/{user_id}/ficha", response_class=HTMLResponse)
def ficha(request: Request, user_id: int, db: Session = Depends(get_db)):
    """Fragmento HTMX del panel lateral (la fila de la tabla lo pide con hx-get)."""
    user = db.get(UserORM, user_id)
    if not user:
        return HTMLResponse(f'<div class="msg error">No existe el usuario id={user_id}.</div>',
                            status_code=404)
    return _TEMPLATES.TemplateResponse(request, "fragments/user_ficha.html", _ctx_ficha(user))
```

En los handlers que quedan de la versión anterior (`add_user`, `delete_user`, `reset_password`, `update_user`): reemplazar TODA llamada directa `_TEMPLATES.TemplateResponse(request, "pages/users.html", {...})` por `_users_page(request, db, error=...)` / `_users_page(request, db, success=...)` con el mismo mensaje y status (el `add_user` tenía dos: "ya existe" y "creado exitosamente"). Las Tasks 6-9 los reescriben; acá sólo tienen que seguir compilando y rendereando la plantilla nueva.

- [ ] **Step 4: `fragments/user_ficha.html`**

```html
{# Ficha de un usuario. Se sirve como fragmento HTMX (GET /users/{id}/ficha) y se
   incluye server-side en pages/users.html cuando viene ?u=<id>.
   Contexto: u (UserORM), v (users_service.vista_usuario), actividad, TABS.
   REGLA anti-XSS: el username va SÓLO en texto o en atributos data-*; nunca dentro
   de un handler on*= (lo fija test_aud_D1_seguridad_web). #}
<div class="um-ficha" data-uid="{{ u.id }}">
  <div class="um-ficha-head">
    <div class="avatar">{{ v.iniciales }}</div>
    <div class="um-ficha-who">
      <div class="um-ficha-user">{{ u.username }}</div>
      <div class="mut">{{ u.full_name or "—" }} ·
        {% if u.is_active %}<span class="badge ok">Activo</span>{% else %}<span class="badge off">Deshabilitado</span>{% endif %}
        {% if v.sin_email %}<span class="badge warn" title="Sin email: no puede recibir links">sin email</span>{% endif %}
      </div>
    </div>
    <a href="/users" class="um-close" title="Cerrar ficha">✕</a>
  </div>

  <div class="um-ficha-body">
    <form method="POST" action="/users/{{ u.id }}/datos" class="um-sect">
      <p class="um-sect-t">Datos</p>
      <div class="um-fields">
        <label class="fld"><span>Nombre</span><input type="text" name="full_name" value="{{ u.full_name or '' }}" maxlength="120" autocomplete="off"></label>
        <label class="fld"><span>Email</span><input type="text" name="email" value="{{ u.email or '' }}" maxlength="254" autocomplete="off" inputmode="email"></label>
        <label class="fld um-span2"><span>Notas internas</span><input type="text" name="notes" value="{{ u.notes or '' }}" maxlength="500" autocomplete="off"></label>
      </div>
      <div class="um-acts"><button type="submit" class="btn">Guardar datos</button></div>
    </form>

    <form method="POST" action="/users/{{ u.id }}/permisos" class="um-sect">
      <p class="um-sect-t">Permisos</p>
      <label class="perm um-admin"><span class="tg"><input type="checkbox" name="is_admin" value="true" {% if u.is_admin %}checked{% endif %} onchange="umToggleTabs(this)"><span class="sl"></span></span>
        <b>Administrador</b> <span class="faint">(acceso total + Manager)</span></label>
      <div class="perm-grid" {% if u.is_admin %}style="opacity: .35; pointer-events: none;"{% endif %}>
        {% set u_tabs = u.allowed_tabs or [] %}
        {% for key, label in TABS %}
        <label class="perm"><span class="tg"><input type="checkbox" name="tabs" value="{{ key }}" {% if key in u_tabs %}checked{% endif %}><span class="sl"></span></span>{{ label }}</label>
        {% endfor %}
      </div>
      <div class="um-acts"><button type="submit" class="btn">Aplicar permisos</button></div>
    </form>

    <div class="um-sect">
      <p class="um-sect-t">Seguridad</p>
      <div class="um-row">
        <button type="button" class="btn sec" data-uid="{{ u.id }}" data-username="{{ u.username }}"
                onclick="resetPassword(this.dataset.uid, this.dataset.username)">Definir contraseña a mano</button>
        <form method="POST" action="/users/{{ u.id }}/sesiones/cerrar"><button type="submit" class="btn sec">Cerrar sesiones</button></form>
        {% if u.is_active %}
        <form method="POST" action="/users/{{ u.id }}/estado"><input type="hidden" name="activo" value="0"><button type="submit" class="btn danger">Deshabilitar cuenta</button></form>
        {% else %}
        <form method="POST" action="/users/{{ u.id }}/estado"><input type="hidden" name="activo" value="1"><button type="submit" class="btn">Habilitar cuenta</button></form>
        {% endif %}
      </div>
      <div class="faint">Contraseña cambiada: {{ v.pwd_cambiada }} · Alta: {{ v.alta }}{% if u.created_by %} por {{ u.created_by }}{% endif %}</div>
    </div>

    <div class="um-sect">
      <p class="um-sect-t">Actividad reciente</p>
      {% for e in actividad %}
      <div class="um-ev"><span class="mono faint">{{ e.texto }}</span><span>{{ e.que }}{% if e.detalle %} <span class="faint">· {{ e.detalle }}</span>{% endif %}</span></div>
      {% else %}
      <div class="faint">Sin actividad registrada todavía.</div>
      {% endfor %}
    </div>

    <div class="um-sect um-foot">
      <form method="POST" action="/users/delete/{{ u.id }}" data-username="{{ u.username }}"
            onsubmit="return confirm('¿Seguro que querés eliminar definitivamente al usuario ' + this.dataset.username + '?');">
        <button type="submit" class="btn danger">Eliminar usuario…</button>
      </form>
    </div>
  </div>
</div>
```

- [ ] **Step 5: `pages/users.html` (reescritura completa)**

```html
{% extends "base.html" %}
{% block title %}Manager de usuarios - Monitor Renta Fija AR{% endblock %}

{% block head %}
<style>
  /* Manager v2 (Opción A): tabla + ficha lateral. Sólo tokens de app.css. */
  .um-wrap { padding: 14px 18px; }
  .um-top { display: flex; align-items: center; gap: 14px; margin-bottom: 12px; }
  .um-top h2 { margin: 0; font-size: 15px; font-weight: 600; }
  .um-top .sum { color: var(--text-dim); font-size: 12px; }
  .um-tools { margin-left: auto; display: flex; gap: 8px; align-items: center; }
  .um-tools input { width: 260px; }
  .um-grid { display: grid; grid-template-columns: minmax(0, 1fr) 440px; gap: 14px; align-items: start; }
  @media (max-width: 1100px) { .um-grid { grid-template-columns: 1fr; } }
  .um-table thead th { text-align: left; padding: 6px 10px; }
  .um-table tbody td { padding: 7px 10px; white-space: normal; vertical-align: middle; }
  .um-table tbody tr { cursor: pointer; }
  .um-table tbody tr.sel td { background: var(--accent-light); }
  .um-hint { padding: 8px 10px; color: var(--text-faint); font-size: 11px; }
  .um-user { display: flex; align-items: center; gap: 10px; }
  .um-user b { font-weight: 600; }
  .avatar { width: 28px; height: 28px; border-radius: 50%; background: var(--accent-light); color: var(--accent);
            display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 11px; flex: none; }
  .mut { color: var(--text-dim); font-size: 12px; } .faint { color: var(--text-faint); font-size: 11px; }
  .mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: 12px; }
  .badge { display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; border-radius: 9999px; font-size: 11px;
           font-weight: 600; border: 1px solid var(--panel-border); color: var(--text-dim); }
  .badge.adm { color: var(--accent); border-color: var(--accent-light); background: var(--accent-light); }
  .badge.ok { color: var(--pos); border-color: rgba(8,153,129,.4); }
  .badge.off { color: var(--text-faint); background: rgba(255,255,255,.03); }
  .badge.warn { color: #e8b339; border-color: rgba(232,179,57,.4); }
  .chip { display: inline-block; padding: 1px 7px; border-radius: 4px; font-size: 11px; background: rgba(255,255,255,.05);
          border: 1px solid var(--panel-border); color: var(--text-dim); margin: 1px 4px 1px 0; }
  .msg { padding: 9px 12px; margin-bottom: 12px; border-radius: 6px; font-size: 13px; }
  .msg.error { background: rgba(242,54,69,.12); color: var(--neg); border: 1px solid rgba(242,54,69,.35); }
  .msg.success { background: rgba(8,153,129,.12); color: var(--pos); border: 1px solid rgba(8,153,129,.35); }
  .btn.sec { background: transparent; color: var(--text); border: 1px solid var(--panel-border); }
  .btn.danger { background: transparent; color: var(--neg); border: 1px solid rgba(242,54,69,.35); }
  /* toggles */
  .tg { position: relative; display: inline-block; width: 30px; height: 16px; flex: none; }
  .tg input { opacity: 0; width: 0; height: 0; position: absolute; }
  .tg .sl { position: absolute; inset: 0; border-radius: 16px; background: var(--panel-border); transition: .2s; }
  .tg .sl::after { content: ""; position: absolute; width: 12px; height: 12px; border-radius: 50%; top: 2px; left: 2px;
                   background: var(--text-dim); transition: .2s; }
  .tg input:checked + .sl { background: var(--accent); }
  .tg input:checked + .sl::after { left: 16px; background: #fff; }
  .perm { display: flex; align-items: center; gap: 8px; font-size: 12px; cursor: pointer; }
  .perm-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px 10px; padding: 10px 12px;
               background: rgba(0,0,0,.18); border-radius: 6px; }
  .um-admin { margin-bottom: 8px; }
  /* ficha */
  .um-ficha-head { display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-bottom: 1px solid var(--panel-border); }
  .um-ficha-head .avatar { width: 34px; height: 34px; font-size: 12px; }
  .um-ficha-who { flex: 1; min-width: 0; } .um-ficha-user { font-weight: 600; }
  .um-close { color: var(--text-faint); text-decoration: none; font-size: 14px; }
  .um-ficha-body { padding: 12px 14px; display: flex; flex-direction: column; gap: 16px; }
  .um-sect-t { font-size: 11px; text-transform: uppercase; letter-spacing: .3px; color: var(--text-faint); font-weight: 600; margin: 0 0 8px; }
  .um-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .um-fields .fld { display: flex; flex-direction: column; gap: 3px; }
  .um-fields .fld span { font-size: 11px; color: var(--text-dim); }
  .um-span2 { grid-column: span 2; }
  .um-acts { margin-top: 10px; display: flex; justify-content: flex-end; }
  .um-row { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
  .um-row form { margin: 0; }
  .um-ev { display: grid; grid-template-columns: 96px 1fr; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--panel-border); font-size: 12px; }
  .um-foot { border-top: 1px solid var(--panel-border); padding-top: 12px; display: flex; justify-content: flex-end; }
  .ficha-empty { padding: 40px 20px; text-align: center; color: var(--text-faint); }
  /* alta */
  .um-add { margin-bottom: 14px; } .um-add summary { list-style: none; display: none; }
  .um-add .panel { border-color: var(--accent); }
</style>
{% endblock %}

{% block content %}
<div class="um-wrap">
  <div class="um-top">
    <div>
      <h2>Usuarios</h2>
      <div class="sum">{{ resumen.total }} usuarios · {{ resumen.activos }} activos · {{ resumen.deshabilitados }} deshabilitados{% if resumen.sin_email %} · {{ resumen.sin_email }} sin email{% endif %}</div>
    </div>
    <div class="um-tools">
      <input type="search" id="um-q" placeholder="Buscar usuario, nombre o email" oninput="umFiltrar(this.value)" autocomplete="off">
      <button type="button" class="btn" onclick="document.getElementById('add-user-panel').toggleAttribute('open')">+ Nuevo usuario</button>
    </div>
  </div>

  {% if error %}<div class="msg error">{{ error }}</div>{% endif %}
  {% if success %}<div class="msg success">{{ success }}</div>{% endif %}

  <details id="add-user-panel" class="um-add"{% if abrir_alta %} open{% endif %}>
    <summary></summary>
    <div class="panel">
      <div class="panel-head" style="cursor: default;">
        <span>Nuevo usuario</span>
        <button type="button" class="um-close" style="background: none; border: none; cursor: pointer;"
                onclick="document.getElementById('add-user-panel').removeAttribute('open')">✕</button>
      </div>
      <form method="POST" action="/users/add" class="um-ficha-body">
        <div class="um-fields">
          <label class="fld"><span>Usuario *</span><input type="text" name="username" required autocomplete="off" maxlength="64"></label>
          <label class="fld"><span>Contraseña inicial *</span>
            <span style="display: flex; gap: 6px;"><input type="password" name="password" id="new-user-pwd" required autocomplete="new-password" minlength="10">
              <button type="button" class="btn sec" onclick="genPwdForInput('new-user-pwd')" title="Generar contraseña segura">Generar</button></span></label>
          <label class="fld"><span>Nombre</span><input type="text" name="full_name" autocomplete="off" maxlength="120"></label>
          <label class="fld"><span>Email</span><input type="text" name="email" autocomplete="off" maxlength="254" inputmode="email"></label>
          <label class="fld um-span2"><span>Notas internas</span><input type="text" name="notes" autocomplete="off" maxlength="500" placeholder="Opcional: quién es, qué pidió"></label>
        </div>
        <div>
          <p class="um-sect-t">Permisos</p>
          <label class="perm um-admin"><span class="tg"><input type="checkbox" name="is_admin" value="true" onchange="umToggleTabs(this)"><span class="sl"></span></span>
            <b>Administrador</b> <span class="faint">(acceso total + Manager)</span></label>
          <div class="perm-grid">
            {% for key, label in TABS %}
            <label class="perm"><span class="tg"><input type="checkbox" name="tabs" value="{{ key }}" {% if key in ("bonos", "on") %}checked{% endif %}><span class="sl"></span></span>{{ label }}</label>
            {% endfor %}
          </div>
        </div>
        <div class="um-acts"><button type="submit" class="btn">Crear usuario</button></div>
      </form>
    </div>
  </details>

  <div class="um-grid">
    <div class="panel">
      <table class="um-table">
        <thead><tr><th>Usuario</th><th>Rol</th><th>Módulos</th><th>Último acceso</th><th>Estado</th></tr></thead>
        <tbody id="um-rows">
          {% for f in filas %}{% set u = f.u %}
          <tr class="{% if selected and selected.id == u.id %}sel{% endif %}"
              data-q="{{ u.username|lower }} {{ (u.full_name or '')|lower }} {{ u.email or '' }}"
              hx-get="/users/{{ u.id }}/ficha" hx-target="#ficha" hx-swap="innerHTML"
              hx-replace-url="/users?u={{ u.id }}" onclick="umSeleccionar(this)">
            <td><div class="um-user"><div class="avatar">{{ f.iniciales }}</div>
              <div><div><b>{{ u.username }}</b>{% if u.full_name %} <span class="mut">· {{ u.full_name }}</span>{% endif %}</div>
                <div class="faint">{{ u.email or "—" }}</div></div></div></td>
            <td>{% if u.is_admin %}<span class="badge adm">Administrador</span>{% else %}<span class="badge">Usuario</span>{% endif %}</td>
            <td>{% if u.is_admin %}<span class="mut" style="font-style: italic;">Acceso total</span>{% else %}{% for t in f.tabs %}<span class="chip">{{ t }}</span>{% else %}<span style="color: var(--neg); font-size: 11px;">Sin accesos</span>{% endfor %}{% endif %}</td>
            <td class="mono mut">{{ f.ultimo_acceso }}</td>
            <td>{% if u.is_active %}<span class="badge ok">Activo</span>{% else %}<span class="badge off">Deshabilitado</span>{% endif %}
                {% if f.sin_email %} <span class="badge warn" title="Sin email: no puede recibir links">sin email</span>{% endif %}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      <div class="um-hint">Clic en una fila abre la ficha a la derecha · la URL guarda la selección (?u=)</div>
    </div>

    <div class="panel" id="ficha">
      {% if selected %}{% include "fragments/user_ficha.html" %}{% else %}<div class="ficha-empty">Elegí un usuario para ver su ficha.</div>{% endif %}
    </div>
  </div>
</div>

<script>
// Contraseñas criptográficamente fuertes para el alta y el reseteo a mano.
function generateSecurePassword(length = 12) {
  const charset = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()_+~";
  const values = new Uint32Array(length);
  window.crypto.getRandomValues(values);
  let out = "";
  for (let i = 0; i < length; i++) out += charset[values[i] % charset.length];
  return out;
}
function genPwdForInput(inputId) {
  const input = document.getElementById(inputId);
  input.value = generateSecurePassword();
  input.type = "text";                                  // mostrarla brevemente
  setTimeout(() => { input.type = "password"; }, 5000);
}
function umToggleTabs(cb) {
  const g = cb.closest("form").querySelector(".perm-grid");
  g.style.opacity = cb.checked ? ".35" : "1";
  g.style.pointerEvents = cb.checked ? "none" : "auto";
}
function umSeleccionar(tr) {
  document.querySelectorAll("#um-rows tr.sel").forEach(r => r.classList.remove("sel"));
  tr.classList.add("sel");
}
function umFiltrar(q) {
  q = (q || "").trim().toLowerCase();
  document.querySelectorAll("#um-rows tr").forEach(tr => {
    tr.style.display = !q || tr.dataset.q.indexOf(q) !== -1 ? "" : "none";
  });
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
// Reseteo a mano: modal propio (sin confirm() ni navigator.clipboard, que sobre HTTP
// no existe). La contraseña queda VISIBLE y seleccionable hasta que el admin confirma;
// el copiado usa window.mrCopy (base.html), que cae a execCommand sin HTTPS.
function resetPassword(userId, username) {
  const pwd = generateSecurePassword();
  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.innerHTML = `
    <div class="modal-card" style="max-width:460px" role="dialog" aria-modal="true" aria-labelledby="rp-title">
      <div class="modal-head"><b id="rp-title">Definir contraseña a mano</b><span class="mut">${escapeHtml(username)}</span></div>
      <div class="modal-body">
        <p style="margin:0 0 4px">Nueva contraseña para <b>${escapeHtml(username)}</b>:</p>
        <div style="display:flex; gap:8px; margin:10px 0">
          <input id="rp-pwd" type="text" readonly value="${escapeHtml(pwd)}"
                 style="flex:1; font-family:var(--font-mono); font-size:15px; letter-spacing:.5px; padding:9px 10px">
          <button type="button" id="rp-copy" class="btn sec">Copiar</button>
        </div>
        <p class="help" style="margin:0">Guardala <b>antes</b> de confirmar: una vez aplicada no se puede volver a ver
           (se almacena hasheada con bcrypt). Al aplicarla se cierran las sesiones abiertas del usuario.</p>
        <div style="display:flex; gap:8px; justify-content:flex-end; margin-top:18px">
          <button type="button" id="rp-cancel" class="btn sec">Cancelar</button>
          <button type="button" id="rp-ok" class="btn">Aplicar ahora</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(ov);
  const close = () => { document.removeEventListener("keydown", onKey); ov.remove(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
  const input = ov.querySelector("#rp-pwd");
  const btnCopy = ov.querySelector("#rp-copy");
  input.focus(); input.select();
  btnCopy.addEventListener("click", () => {
    window.mrCopy(pwd, (ok) => {
      btnCopy.textContent = ok ? "¡Copiado!" : "Copiá con Ctrl+C";
      input.focus(); input.select();
      setTimeout(() => { btnCopy.textContent = "Copiar"; }, 2500);
    });
  });
  ov.querySelector("#rp-cancel").addEventListener("click", close);
  ov.querySelector("#rp-ok").addEventListener("click", () => { close(); submitReset(userId, pwd); });
}
function submitReset(userId, pwd) {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = `/users/${userId}/reset`;
  for (const [name, value] of [["channel", "manual"], ["password", pwd]]) {
    const i = document.createElement("input");
    i.type = "hidden"; i.name = name; i.value = value;
    form.appendChild(i);
  }
  document.body.appendChild(form);
  form.submit();
}
</script>
{% endblock %}
```

- [ ] **Step 6: Correr**

Run: `py -3.12 -m pytest tests/test_users_manager.py tests/test_aud_D1_seguridad_web.py tests/test_sec_audit_y_passwords.py -q`
Expected: los 3 nuevos PASS; `test_users_page_no_mete_el_username_en_atributos_de_evento` PASS; el resto igual que antes de la task (los tests de `/users/update` y `/users/reset-password` siguen verdes porque esos handlers aún existen).

- [ ] **Step 7: Commit**

```bash
git add apps/web/routers/users_abm.py apps/web/templates/pages/users.html apps/web/templates/fragments/user_ficha.html tests/test_users_manager.py
git commit -m "Manager v2: tabla + ficha lateral HTMX (Opción A), GET /users?u= y /users/{id}/ficha"
```

---

### Task 6: `POST /users/{id}/datos` y `POST /users/{id}/permisos`

**Files:**
- Modify: `apps/web/routers/users_abm.py` (nuevo `update_datos`; `update_user` → ruta `/users/{user_id}/permisos`)
- Modify: `tests/test_aud_D1_seguridad_web.py:118` y `tests/test_sec_audit_y_passwords.py:215,265` (ruta nueva)
- Test: `tests/test_users_manager.py`

**Interfaces:**
- Consumes: `_users_page`, `_no_existe`, `_texto`, `_tabs_validas`, `normalizar_email`, `email_invalido` (Tasks 3 y 5).
- Produces: `POST /users/{user_id}/datos` (form `full_name, email, notes`) → 200 con la ficha abierta, 400 si el email es inválido o ya lo tiene otro usuario, 404 si no existe. `POST /users/{user_id}/permisos` (form `is_admin, tabs`) con los guards del último admin y la auditoría `action=update` intacta.

- [ ] **Step 1: Tests**

Agregar a `tests/test_users_manager.py`:

```python
# ── POST datos / permisos ───────────────────────────────────────────────────
@pytest.mark.noauth
def test_datos_guarda_normaliza_y_mantiene_la_ficha_abierta(usuarios):
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post(f"/users/{bob}/datos",
                   data={"full_name": "  Roberto Pérez ", "email": " Bob.Perez@Ejemplo.COM ",
                         "notes": "cliente"})
    assert r.status_code == 200 and f'action="/users/{bob}/datos"' in r.text
    with SessionLocal() as s:
        b = s.get(UserORM, bob)
        assert (b.full_name, b.email, b.notes) == ("Roberto Pérez", "bob.perez@ejemplo.com", "cliente")


@pytest.mark.noauth
def test_datos_rechaza_email_invalido_o_duplicado(usuarios):
    bob = _bob_id()
    with SessionLocal() as s:
        s.query(UserORM).filter(UserORM.username == "admin").first().email = "admin@ejemplo.com"
        s.commit()
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post(f"/users/{bob}/datos", data={"email": "sin-arroba"}).status_code == 400
        assert c.post(f"/users/{bob}/datos", data={"email": "ADMIN@ejemplo.com"}).status_code == 400
        assert c.post("/users/999999/datos", data={"email": ""}).status_code == 404
        # vaciar el email es válido
        assert c.post(f"/users/{bob}/datos", data={"email": ""}).status_code == 200
    with SessionLocal() as s:
        assert s.get(UserORM, bob).email is None


@pytest.mark.noauth
def test_permisos_reemplaza_a_update_y_conserva_los_guards(usuarios):
    bob = _bob_id()
    with SessionLocal() as s:
        admin_id = s.query(UserORM).filter(UserORM.username == "admin").first().id
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post(f"/users/{bob}/permisos", data={"tabs": ["bonos", "fci", "inventada"]}).status_code == 200
        r = c.post(f"/users/{admin_id}/permisos", data={"is_admin": "false", "tabs": ["bonos"]})
        assert "último administrador" in r.text
        assert c.post("/users/999999/permisos", data={"tabs": ["bonos"]}).status_code == 404
    with SessionLocal() as s:
        assert s.get(UserORM, bob).allowed_tabs == ["bonos", "fci"]     # la inventada no entra
        assert s.get(UserORM, admin_id).is_admin is True
```

- [ ] **Step 2: Correr y ver que fallan**

Run: `py -3.12 -m pytest tests/test_users_manager.py -q -k "datos or permisos"`
Expected: 3 FAIL (404/405 en `/datos` y `/permisos`).

- [ ] **Step 3: Implementar**

En `users_abm.py`, agregar (y renombrar la ruta de `update_user`):

```python
@router.post("/users/{user_id}/datos", response_class=HTMLResponse)
def update_datos(request: Request, user_id: int, full_name: str = Form(""), email: str = Form(""),
                 notes: str = Form(""), db: Session = Depends(get_db),
                 admin: UserORM = Depends(get_admin_user_html)):
    user = db.get(UserORM, user_id)
    if not user:
        return _no_existe(request, db, user_id)
    mail = normalizar_email(email)
    invalido = email_invalido(mail)
    if invalido:
        return _users_page(request, db, status_code=400, selected_id=user_id, error=invalido)
    if mail and db.query(UserORM).filter(UserORM.email == mail, UserORM.id != user_id).first():
        return _users_page(request, db, status_code=400, selected_id=user_id,
                           error=f"Ya hay otro usuario con el email {mail}.")
    user.full_name = _texto(full_name, _NOMBRE_MAX)
    user.email = mail
    user.notes = _texto(notes, _NOTAS_MAX)
    db.commit()
    _audit.info("users action=datos by=%s target=%s email=%s",
                _limpio(getattr(admin, "username", "?")), _limpio(user.username),
                _limpio(mail or "-"), extra={"console": True})
    return _users_page(request, db, selected_id=user_id,
                       success=f"Datos actualizados para {user.username}.")


@router.post("/users/{user_id}/permisos", response_class=HTMLResponse)
def update_permisos(
    request: Request,
    user_id: int,
    is_admin: bool = Form(False),
    tabs: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
    admin: UserORM = Depends(get_admin_user_html),
):
    user = db.get(UserORM, user_id)
    if not user:
        return _no_existe(request, db, user_id)

    # No quitarle el rol al último admin
    if user.is_admin and not is_admin:
        admins = db.query(UserORM).filter(UserORM.is_admin.is_(True)).count()
        if admins <= 1:
            return _users_page(request, db, selected_id=user_id,
                               error="No puedes quitarle el rol de admin al último administrador.")

    antes_admin, antes_tabs = user.is_admin, list(user.allowed_tabs or [])
    user.is_admin = is_admin
    user.allowed_tabs = ["*"] if is_admin else _tabs_validas(tabs)
    db.commit()
    # La PROMOCIÓN A ADMIN es la acción más sensible de la ABM: se loguea el estado
    # ANTES y DESPUÉS ("quién tenía qué rol" es lo que se reconstruye tras un incidente).
    _audit.info("users action=update by=%s target=%s is_admin=%s->%s tabs=%s->%s",
                _limpio(getattr(admin, "username", "?")), _limpio(user.username),
                bool(antes_admin), bool(is_admin),
                _limpio(",".join(antes_tabs)),
                _limpio(",".join(user.allowed_tabs or [])), extra={"console": True})
    return _users_page(request, db, selected_id=user_id,
                       success=f"Permisos actualizados para {user.username}.")
```

Borrar el viejo `update_user` (`@router.post("/users/update/{user_id}")`).

- [ ] **Step 4: Actualizar los tests viejos a la ruta nueva**

- `tests/test_aud_D1_seguridad_web.py:118`: `r2 = c.post("/users/999999/permisos", data={"is_admin": "false"})` y el mensaje del assert: `f"permisos devolvió {r2.status_code}"`.
- `tests/test_sec_audit_y_passwords.py:215`: `admin_c.post(f"/users/{bob_id}/permisos", data={"tabs": ["bonos", "fci"]})`.
- `tests/test_sec_audit_y_passwords.py:265`: `c.post(f"/users/{bob}/permisos", data={"is_admin": "true"})`.

- [ ] **Step 5: Correr**

Run: `py -3.12 -m pytest tests/test_users_manager.py tests/test_aud_D1_seguridad_web.py tests/test_sec_audit_y_passwords.py -q`
Expected: todo PASS. `test_la_promocion_a_admin_QUEDA_registrada` sigue verde (misma línea `action=update`).

- [ ] **Step 6: Commit**

```bash
git add apps/web/routers/users_abm.py tests/test_users_manager.py tests/test_aud_D1_seguridad_web.py tests/test_sec_audit_y_passwords.py
git commit -m "Manager v2: POST /users/{id}/datos y /permisos (reemplaza /users/update)"
```

---

### Task 7: `POST /users/{id}/reset` (canal manual) y `POST /users/{id}/sesiones/cerrar`

**Files:**
- Modify: `apps/web/routers/users_abm.py` (`reset_password` → ruta y form nuevos; nuevo `cerrar_sesiones`)
- Modify: `tests/test_aud_D1_seguridad_web.py:117`, `tests/test_sec_audit_y_passwords.py:101,142,147,188`
- Test: `tests/test_users_manager.py`

**Interfaces:**
- Produces: `POST /users/{user_id}/reset` con form `channel` (`manual` en esta fase; cualquier otro valor → 400 "no disponible todavía") y `password`; escribe `hashed_password`, `password_changed_at`, `token_version += 1`; auditoría `action=reset_password ... channel=manual`. `POST /users/{user_id}/sesiones/cerrar` → `token_version += 1`, auditoría `action=sessions_closed`.

- [ ] **Step 1: Tests**

```python
# ── reset manual / cerrar sesiones ──────────────────────────────────────────
@pytest.mark.noauth
def test_reset_manual_cambia_la_clave_fecha_y_cierra_sesiones(usuarios):
    bob = _bob_id()
    with TestClient(app) as bob_c, TestClient(app) as admin_c:
        assert _login(bob_c, "bob", "bobpass1234").status_code in (302, 303)
        _login_admin(admin_c)
        r = admin_c.post(f"/users/{bob}/reset", data={"channel": "manual", "password": "nuevaclave1"})
        assert r.status_code == 200
        assert bob_c.get("/", follow_redirects=False).status_code == 302   # sesión muerta
        assert _login(bob_c, "bob", "nuevaclave1").status_code in (302, 303)
    with SessionLocal() as s:
        assert s.get(UserORM, bob).password_changed_at is not None


@pytest.mark.noauth
def test_reset_valida_despues_del_lookup_y_rechaza_canales_futuros(usuarios):
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post("/users/999999/reset", data={"channel": "manual", "password": "x"}).status_code == 404
        assert c.post(f"/users/{bob}/reset", data={"channel": "manual", "password": "x"}).status_code == 400
        assert c.post(f"/users/{bob}/reset", data={"channel": "mail"}).status_code == 400


@pytest.mark.noauth
def test_cerrar_sesiones_saca_al_usuario_sin_cambiarle_la_clave(usuarios):
    bob = _bob_id()
    with TestClient(app) as bob_c, TestClient(app) as admin_c:
        assert _login(bob_c, "bob", "bobpass1234").status_code in (302, 303)
        _login_admin(admin_c)
        assert admin_c.post(f"/users/{bob}/sesiones/cerrar").status_code == 200
        assert bob_c.get("/", follow_redirects=False).status_code == 302
        assert _login(bob_c, "bob", "bobpass1234").status_code in (302, 303)   # misma clave
        assert admin_c.post("/users/999999/sesiones/cerrar").status_code == 404
```

- [ ] **Step 2: Correr y ver que fallan**

Run: `py -3.12 -m pytest tests/test_users_manager.py -q -k "reset or cerrar_sesiones"`
Expected: 3 FAIL.

- [ ] **Step 3: Implementar**

Reemplazar el viejo `reset_password` por:

```python
@router.post("/users/{user_id}/reset", response_class=HTMLResponse)
def reset_password(request: Request, user_id: int, channel: str = Form("manual"),
                   password: str = Form(""), db: Session = Depends(get_db),
                   admin: UserORM = Depends(get_admin_user_html)):
    """Canal `manual` (el admin define la contraseña). Los canales `link` y `mail`
    llegan en las Fases 2 y 3 (spec §5.1)."""
    user = db.get(UserORM, user_id)
    if not user:
        return _no_existe(request, db, user_id)
    if channel != "manual":
        return _users_page(request, db, status_code=400, selected_id=user_id,
                           error="Ese canal de reseteo no está disponible todavía.")
    # La validación va DESPUÉS del lookup a propósito: un id inexistente da 404 aunque
    # la contraseña también sea inválida (lo fija test_aud_D1).
    invalida = password_invalida(password)
    if invalida:
        return _users_page(request, db, status_code=400, selected_id=user_id, error=invalida)

    user.hashed_password = get_password_hash(password)
    user.password_changed_at = datetime.now()
    # Cierra las sesiones abiertas de ese usuario. NO se hace en `update_permisos`: los
    # permisos se releen de la base en cada request, así que una degradación ya es
    # inmediata y bumpear ahí sólo desloguearía gente sin comprar nada.
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    _audit.info("users action=reset_password by=%s target=%s channel=manual",
                _limpio(getattr(admin, "username", "?")), _limpio(user.username),
                extra={"console": True})
    return _users_page(request, db, selected_id=user_id,
                       success=f"Contraseña actualizada para {user.username}. Sus sesiones se cerraron.")


@router.post("/users/{user_id}/sesiones/cerrar", response_class=HTMLResponse)
def cerrar_sesiones(request: Request, user_id: int, db: Session = Depends(get_db),
                    admin: UserORM = Depends(get_admin_user_html)):
    user = db.get(UserORM, user_id)
    if not user:
        return _no_existe(request, db, user_id)
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    _audit.info("users action=sessions_closed by=%s target=%s",
                _limpio(getattr(admin, "username", "?")), _limpio(user.username),
                extra={"console": True})
    return _users_page(request, db, selected_id=user_id,
                       success=f"Sesiones de {user.username} cerradas.")
```

Agregar `from datetime import datetime` a los imports del módulo.

- [ ] **Step 4: Actualizar los tests viejos**

- `tests/test_aud_D1_seguridad_web.py:117`: `r1 = c.post("/users/999999/reset", data={"channel": "manual", "password": "x"})`; assert: `f"reset devolvió {r1.status_code}"`.
- `tests/test_sec_audit_y_passwords.py:101`: `c.post(f"/users/{bob}/reset", data={"channel": "manual", "password": "otraclave12"})`.
- `tests/test_sec_audit_y_passwords.py:142`: `c.post("/users/999999/reset", data={"channel": "manual", "password": "x"})`.
- `tests/test_sec_audit_y_passwords.py:147`: `c.post(f"/users/{bob_id}/reset", data={"channel": "manual", "password": "x"})`.
- `tests/test_sec_audit_y_passwords.py:188`: `admin_c.post(f"/users/{bob_id}/reset", data={"channel": "manual", "password": "nuevaclave1"})`.

- [ ] **Step 5: Correr**

Run: `py -3.12 -m pytest tests/test_users_manager.py tests/test_aud_D1_seguridad_web.py tests/test_sec_audit_y_passwords.py -q`
Expected: todo PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/web/routers/users_abm.py tests/test_users_manager.py tests/test_aud_D1_seguridad_web.py tests/test_sec_audit_y_passwords.py
git commit -m "Manager v2: POST /users/{id}/reset (canal manual) y /sesiones/cerrar"
```

---

### Task 8: `POST /users/{id}/estado` (deshabilitar / habilitar)

**Files:**
- Modify: `apps/web/routers/users_abm.py`
- Test: `tests/test_users_manager.py`

**Interfaces:**
- Produces: `POST /users/{user_id}/estado` con form `activo` (`"0"`/`"1"`). Deshabilitar: 400 si es uno mismo o el último admin activo; sube `token_version` (sesiones muertas de inmediato) y audita `action=user_disabled`. Habilitar: `action=user_enabled`.

- [ ] **Step 1: Tests**

```python
# ── estado ──────────────────────────────────────────────────────────────────
@pytest.mark.noauth
def test_deshabilitar_y_habilitar_desde_el_manager(usuarios):
    bob = _bob_id()
    with TestClient(app) as bob_c, TestClient(app) as admin_c:
        assert _login(bob_c, "bob", "bobpass1234").status_code in (302, 303)
        _login_admin(admin_c)
        r = admin_c.post(f"/users/{bob}/estado", data={"activo": "0"})
        assert r.status_code == 200 and "Habilitar cuenta" in r.text
        assert bob_c.get("/", follow_redirects=False).status_code == 302
        assert _login(bob_c, "bob", "bobpass1234").status_code == 200        # no entra
        r = admin_c.post(f"/users/{bob}/estado", data={"activo": "1"})
        assert r.status_code == 200 and "Deshabilitar cuenta" in r.text
        assert _login(bob_c, "bob", "bobpass1234").status_code in (302, 303)
        assert admin_c.post("/users/999999/estado", data={"activo": "0"}).status_code == 404


@pytest.mark.noauth
def test_no_se_puede_deshabilitar_a_uno_mismo_y_siempre_queda_un_admin_activo(usuarios):
    """El guard de "uno mismo" es el que se ejercita: como quien pide es un admin ACTIVO,
    el único caso de "último admin activo" es deshabilitarse a sí mismo. El guard de
    último admin del handler queda como defensa en profundidad (p. ej. con el admin
    falso de `_auth_bypass`, que no vive en la DB)."""
    with SessionLocal() as s:
        admin_id = s.query(UserORM).filter(UserORM.username == "admin").first().id
        s.add(UserORM(username="admin2", hashed_password=get_password_hash("adminpass2"),
                      is_admin=True, allowed_tabs=["*"], is_active=True))
        s.commit()
        admin2_id = s.query(UserORM).filter(UserORM.username == "admin2").first().id
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post(f"/users/{admin_id}/estado", data={"activo": "0"})
        assert r.status_code == 400 and "tu propia cuenta" in r.text
        assert c.post(f"/users/{admin2_id}/estado", data={"activo": "0"}).status_code == 200
        # admin2 quedó inactivo → admin es el ÚLTIMO admin activo; con admin2 logueado
        # no se lo podría deshabilitar. Se simula desde admin2 rehabilitado:
        c.post(f"/users/{admin2_id}/estado", data={"activo": "1"})
    with TestClient(app) as c2:
        assert _login(c2, "admin2", "adminpass2").status_code in (302, 303)
        c2.post(f"/users/{admin_id}/estado", data={"activo": "0"})           # deja a admin2 solo
        r = c2.post(f"/users/{admin2_id}/estado", data={"activo": "0"})
        assert r.status_code == 400 and "tu propia cuenta" in r.text
    with SessionLocal() as s:
        activos = s.query(UserORM).filter(UserORM.is_admin.is_(True), UserORM.is_active.is_(True)).count()
        assert activos >= 1


@pytest.mark.noauth
def test_un_usuario_comun_no_puede_tocar_el_manager(usuarios):
    """Todas las rutas del Manager (ya existen todas al llegar acá) exigen admin: un
    usuario común logueado recibe 403, nunca 302 ni 200."""
    bob = _bob_id()
    with TestClient(app) as c:
        assert _login(c, "bob", "bobpass1234").status_code in (302, 303)
        for path in (f"/users/{bob}/datos", f"/users/{bob}/permisos", f"/users/{bob}/reset",
                     f"/users/{bob}/sesiones/cerrar", f"/users/{bob}/estado", "/users/add"):
            r = c.post(path, data={"activo": "1", "channel": "manual", "password": "x" * 10,
                                   "username": "z", "tabs": ["bonos"]}, follow_redirects=False)
            assert r.status_code == 403, f"{path} devolvió {r.status_code}"
        assert c.get("/users", follow_redirects=False).status_code == 403
        assert c.get(f"/users/{bob}/ficha", follow_redirects=False).status_code == 403
```

- [ ] **Step 2: Correr y ver que fallan**

Run: `py -3.12 -m pytest tests/test_users_manager.py -q -k "estado or uno_mismo or comun"`
Expected: FAIL (405 en `/estado`; el del usuario común falla en `/estado` con 405).

- [ ] **Step 3: Implementar**

```python
@router.post("/users/{user_id}/estado", response_class=HTMLResponse)
def set_estado(request: Request, user_id: int, activo: str = Form(...),
               db: Session = Depends(get_db), admin: UserORM = Depends(get_admin_user_html)):
    """Deshabilitar ≠ borrar: los datos quedan, el usuario no entra y sus sesiones
    mueren (token_version + chequeo de is_active en deps_auth)."""
    user = db.get(UserORM, user_id)
    if not user:
        return _no_existe(request, db, user_id)
    activar = activo == "1"
    if not activar:
        if user.id == getattr(admin, "id", None):
            return _users_page(request, db, status_code=400, selected_id=user_id,
                               error="No podés deshabilitar tu propia cuenta.")
        if user.is_admin:
            # Defensa en profundidad: quien pide ya es un admin activo distinto del
            # target, así que en la práctica siempre quedan ≥ 2; cubre al admin falso de
            # los tests (`_auth_bypass`), que no está en la DB.
            admins_activos = db.query(UserORM).filter(
                UserORM.is_admin.is_(True), UserORM.is_active.is_(True)).count()
            if admins_activos <= 1:
                return _users_page(request, db, status_code=400, selected_id=user_id,
                                   error="No podés deshabilitar al último administrador activo.")
    user.is_active = activar
    if not activar:
        user.token_version = (user.token_version or 0) + 1
    db.commit()
    _audit.info("users action=%s by=%s target=%s",
                "user_enabled" if activar else "user_disabled",
                _limpio(getattr(admin, "username", "?")), _limpio(user.username),
                extra={"console": True})
    return _users_page(request, db, selected_id=user_id,
                       success=f"Cuenta de {user.username} {'habilitada' if activar else 'deshabilitada'}.")
```

- [ ] **Step 4: Correr**

Run: `py -3.12 -m pytest tests/test_users_manager.py tests/test_aud_G_tests_route_auth.py -q`
Expected: todo PASS, incluido `test_un_usuario_comun_no_puede_tocar_el_manager` y el guard de rutas.

- [ ] **Step 5: Commit**

```bash
git add apps/web/routers/users_abm.py tests/test_users_manager.py
git commit -m "Manager v2: deshabilitar/habilitar cuenta con guards de último admin y de uno mismo"
```

---

### Task 9: `POST /users/add` con nombre, email, notas y trazabilidad

**Files:**
- Modify: `apps/web/routers/users_abm.py` (`add_user`)
- Test: `tests/test_users_manager.py`

**Interfaces:**
- Produces: `POST /users/add` con form `username, password, full_name, email, notes, is_admin, tabs`. Escribe `created_at`, `created_by` (username del admin), `password_changed_at`, `is_active=True`. 400 si username/contraseña/email inválidos, username duplicado o email duplicado. Responde con la ficha del nuevo usuario abierta.

- [ ] **Step 1: Tests**

```python
# ── alta ────────────────────────────────────────────────────────────────────
@pytest.mark.noauth
def test_alta_guarda_perfil_y_trazabilidad(usuarios):
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post("/users/add", data={"username": "mcaceres", "password": "clave-segura-1",
                                       "full_name": "Mariana Cáceres", "email": "M.Caceres@Ejemplo.com",
                                       "notes": "cliente", "tabs": ["bonos", "fci"]})
    assert r.status_code == 200 and 'action="/users/' in r.text and "mcaceres" in r.text
    with SessionLocal() as s:
        m = s.query(UserORM).filter(UserORM.username == "mcaceres").first()
        assert m.email == "m.caceres@ejemplo.com" and m.full_name == "Mariana Cáceres"
        assert m.created_by == "admin" and m.created_at is not None
        assert m.password_changed_at is not None and m.is_active is True
        assert m.allowed_tabs == ["bonos", "fci"]


@pytest.mark.noauth
def test_alta_rechaza_duplicados_y_email_invalido(usuarios):
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post("/users/add", data={"username": "bob", "password": "clave-segura-1"}).status_code == 400
        assert c.post("/users/add", data={"username": "otro", "password": "clave-segura-1",
                                          "email": "bob@ejemplo.com"}).status_code == 400
        assert c.post("/users/add", data={"username": "otro", "password": "clave-segura-1",
                                          "email": "mal"}).status_code == 400
    with SessionLocal() as s:
        assert s.query(UserORM).filter(UserORM.username == "otro").first() is None
```

- [ ] **Step 2: Correr y ver que fallan**

Run: `py -3.12 -m pytest tests/test_users_manager.py -q -k alta`
Expected: FAIL (`email`/`created_by` en None; duplicado devuelve 200).

- [ ] **Step 3: Implementar** (reemplaza el `add_user` anterior)

```python
@router.post("/users/add", response_class=HTMLResponse)
def add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    is_admin: bool = Form(False),
    tabs: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
    admin: UserORM = Depends(get_admin_user_html),
):
    mail = normalizar_email(email)
    invalido = _username_invalido(username) or password_invalida(password) or email_invalido(mail)
    if invalido:
        return _users_page(request, db, status_code=400, abrir_alta=True, error=invalido)
    if db.query(UserORM).filter(UserORM.username == username).first():
        return _users_page(request, db, status_code=400, abrir_alta=True,
                           error=f"El usuario {username} ya existe.")
    if mail and db.query(UserORM).filter(UserORM.email == mail).first():
        return _users_page(request, db, status_code=400, abrir_alta=True,
                           error=f"Ya hay un usuario con el email {mail}.")

    ahora = datetime.now()
    new_user = UserORM(
        username=username,
        hashed_password=get_password_hash(password),
        is_admin=is_admin,
        allowed_tabs=["*"] if is_admin else _tabs_validas(tabs),
        full_name=_texto(full_name, _NOMBRE_MAX),
        email=mail,
        notes=_texto(notes, _NOTAS_MAX),
        is_active=True,
        created_at=ahora,
        created_by=getattr(admin, "username", None),
        password_changed_at=ahora,
    )
    db.add(new_user)
    db.commit()
    _audit.info("users action=add by=%s target=%s is_admin=%s tabs=%s email=%s",
                _limpio(getattr(admin, "username", "?")), _limpio(username),
                bool(is_admin), _limpio(",".join(new_user.allowed_tabs or [])),
                _limpio(mail or "-"), extra={"console": True})
    return _users_page(request, db, selected_id=new_user.id,
                       success=f"Usuario {username} creado.")
```

(`abrir_alta` lo consume el `<details ... {% if abrir_alta %} open{% endif %}>` de `users.html` para que el form no se cierre tras un error.)

- [ ] **Step 4: Correr**

Run: `py -3.12 -m pytest tests/test_users_manager.py tests/test_sec_audit_y_passwords.py tests/test_aud_D1_seguridad_web.py tests/test_sec_csrf_y_headers.py tests/test_perf_W1_ingest_auth_session.py -q`
Expected: todo PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/web/routers/users_abm.py tests/test_users_manager.py
git commit -m "Manager v2: alta con nombre, email, notas y trazabilidad (creado por / cuándo)"
```

---

### Task 10: `base_public.html` y login con el look de la app

**Files:**
- Create: `apps/web/templates/base_public.html`
- Rewrite: `apps/web/templates/pages/login.html`
- Test: `tests/test_users_manager.py`

**Interfaces:**
- Produces: plantilla `base_public.html` con bloques `title`, `head`, `header_meta`, `content`, header de la app SIN nav ni badge (no hay sesión: nada que pegue a rutas privadas) y tarjeta `.pub-card`. Las Fases 2 y 3 (`/reset/{token}`, `/forgot`) la extienden.

- [ ] **Step 1: Test**

```python
# ── login con el look de la app ─────────────────────────────────────────────
@pytest.mark.noauth
def test_login_usa_los_tokens_y_el_header_de_la_app(usuarios):
    with TestClient(app) as c:
        r = c.get("/login")
    assert r.status_code == 200
    assert "/static/css/app.css" in r.text
    assert "MONITOR · Renta Fija AR" in r.text
    for var in ("var(--bg)", "var(--surface)", "var(--border)"):
        assert var not in r.text, f"login.html sigue usando la variable inexistente {var}"
    assert "/health/badge" not in r.text and "hx-get" not in r.text   # sin nav ni polling privado
```

- [ ] **Step 2: Correr y ver que falla**

Run: `py -3.12 -m pytest tests/test_users_manager.py -q -k login_usa`
Expected: FAIL (`var(--bg)` presente).

- [ ] **Step 3: `base_public.html`**

```html
{# Base de las páginas SIN sesión (login; en las Fases 2-3 /forgot y /reset/{token}).
   Mismo header y tokens que base.html, pero SIN nav, badge de frescura ni menú de
   fuente: todo eso pega a rutas privadas y un anónimo rebotaría a /login. #}
<!doctype html>
<html lang="es" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Acceso - Monitor Renta Fija AR{% endblock %}</title>
  <link rel="icon" type="image/svg+xml" href="/static/icons/icon.svg">
  <meta name="theme-color" content="#0f172a">
  <link rel="stylesheet" href="/static/css/app.css">
  <script>
    // Tema guardado antes del primer paint (mismo mecanismo que base.html).
    (function () {
      var t = localStorage.getItem("theme");
      if (t) document.documentElement.setAttribute("data-theme", t);
    })();
  </script>
  <style>
    .pub-main { min-height: calc(100vh - 41px); display: flex; align-items: center; justify-content: center; padding: 30px 20px; }
    .pub-card { background: var(--panel-bg); border: 1px solid var(--panel-border); border-radius: 8px; padding: 28px 32px;
                width: 100%; max-width: 400px; box-shadow: 0 10px 30px rgba(0,0,0,.35); }
    .pub-card h2 { margin: 0 0 4px; font-size: 18px; font-weight: 600; letter-spacing: .2px; }
    .pub-card .sub { color: var(--text-dim); font-size: 12.5px; margin: 0 0 18px; }
    .pub-card .fld { display: flex; flex-direction: column; gap: 4px; margin-bottom: 14px; }
    .pub-card .fld span { font-size: 11px; color: var(--text-dim); }
    .pub-card input { width: 100%; padding: 10px 11px; font-size: 14px; background: var(--page-bg); }
    .pub-card button[type="submit"] { width: 100%; padding: 10px 15px; font-size: 14px; }
    .pub-card .links { text-align: center; margin-top: 14px; font-size: 12.5px; }
    .pub-card a { color: var(--accent); text-decoration: none; }
    .pub-card a:hover { text-decoration: underline; }
    .msg { padding: 9px 12px; border-radius: 6px; font-size: 13px; margin-bottom: 16px; text-align: center; }
    .msg.error { background: rgba(242,54,69,.12); color: var(--neg); border: 1px solid rgba(242,54,69,.35); }
    .msg.success { background: rgba(8,153,129,.12); color: var(--pos); border: 1px solid rgba(8,153,129,.35); }
  </style>
  {% block head %}{% endblock %}
</head>
<body>
  <header>
    <div class="header-top">
      <h1>MONITOR · Renta Fija AR</h1>
      <span class="meta" style="margin-left: auto;">{% block header_meta %}Acceso{% endblock %}</span>
    </div>
  </header>
  <main class="pub-main">
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

- [ ] **Step 4: `login.html`**

```html
{% extends "base_public.html" %}
{% block content %}
<div class="pub-card">
  <h2>Ingresar</h2>
  <p class="sub">Monitor de renta fija argentina.</p>
  {% if error %}<div class="msg error">{{ error }}</div>{% endif %}
  <form method="POST" action="/login">
    <label class="fld"><span>Usuario</span>
      <input type="text" name="username" required autofocus autocomplete="username"></label>
    <label class="fld"><span>Contraseña</span>
      <input type="password" name="password" required autocomplete="current-password"></label>
    <button type="submit">Ingresar</button>
  </form>
</div>
{% endblock %}
```

- [ ] **Step 5: Correr**

Run: `py -3.12 -m pytest tests/test_users_manager.py tests/test_auth.py tests/test_sec_csrf_y_headers.py -q`
Expected: todo PASS (los tests de login sólo miran status, cookie y el texto de error, que no cambia).

- [ ] **Step 6: Mirarlo** con `/smoke` (app en :8001, `GET /login` y `GET /users` logueado) o `/verificar-ui`: header, tarjeta centrada, tema dark/light; en `/users` clic en una fila abre la ficha y la URL pasa a `?u=`.

- [ ] **Step 7: Commit**

```bash
git add apps/web/templates/base_public.html apps/web/templates/pages/login.html tests/test_users_manager.py
git commit -m "Login con el header y los tokens de la app (base_public.html para las páginas sin sesión)"
```

---

### Task 11: Docs, gate, security-review y cierre de fase

**Files:**
- Modify: `.claude/rules/auth.md` (frontmatter `paths` + sección Usuarios)
- Modify: `docs/auth.md` (sección nueva "Manager de usuarios")
- Modify: `docs/decisiones.md` (D5)

- [ ] **Step 1: `.claude/rules/auth.md`**

En el frontmatter agregar `- "apps/web/users_service.py"` a `paths`. Reemplazar la sección `## Usuarios` por:

```markdown
## Usuarios (Manager v2, spec 2026-09-08)

- Rutas del admin (`routers/users_abm.py`, todas bajo `get_admin_user_html`): `GET /users[?u=id]`,
  `GET /users/{id}/ficha` (fragmento HTMX), `POST /users/add`, `POST /users/{id}/datos`,
  `/permisos`, `/reset` (form `channel`: `manual`; `link`/`mail` en Fases 2-3), `/sesiones/cerrar`,
  `/estado` (form `activo` 0/1), `POST /users/delete/{id}`. Las viejas `/users/update/{id}` y
  `/users/reset-password/{id}` ya no existen.
- **Toda respuesta HTML de la ABM pasa por `_users_page`** (arma filas, resumen y ficha
  seleccionada); no llamar `TemplateResponse("pages/users.html")` a mano.
- Reglas puras (email, estado, actividad derivada, `TABS`) en `apps/web/users_service.py`, sin
  FastAPI: se testean solas.
- `is_active=0` (deshabilitado): el login responde EXACTAMENTE igual que una clave incorrecta
  (no confirma que la cuenta existe) y `deps_auth._get_user_from_token` rechaza la cookie aunque
  la `token_version` coincida. No se puede deshabilitar a uno mismo ni al último admin activo.
- Resetear la contraseña (canal manual) o "Cerrar sesiones" suben `token_version` y cierran las
  sesiones de ese usuario (commit `5452c3f`); cambiar permisos NO (se releen por request).
- Política de contraseña única: `core/security.password_invalida` (10 chars / 72 bytes).
- No hay bootstrap automático del admin: la única receta es `scripts/init_admin.py` con
  `MONITOR_ADMIN_PASSWORD` (`docs/despliegue.md › Primer arranque`). Nunca un default
  hardcodeado.
```

- [ ] **Step 2: `docs/auth.md`**

Agregar antes de `## Al testear la web`:

```markdown
## Manager de usuarios

Rediseñado el 2026-09-08 (spec `docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md`,
Opción A: tabla + ficha lateral HTMX). `users` tiene email (único si no es NULL, índice parcial
`ux_users_email`), nombre, notas, `is_active`, alta (cuándo/quién), último ingreso (fecha/IP) y
fecha del último cambio de contraseña; todo entró por la migración forward-only de `init_db`.
Las reglas puras viven en `apps/web/users_service.py`; el router `routers/users_abm.py` tiene una
ruta POST por acción y responde siempre con `_users_page`. Deshabilitar una cuenta bloquea el
login (misma respuesta que una clave incorrecta) y mata la sesión viva en el siguiente request.
Las páginas sin sesión (`/login`; en las fases siguientes `/forgot` y `/reset/{token}`) extienden
`templates/base_public.html`: header de la app sin nav. Fases 2 y 3 (tokens, link copiable,
mail, autoservicio) están descritas en la spec.
```

- [ ] **Step 3: `docs/decisiones.md`**

Agregar antes de `## Decisiones ya tomadas durante la auditoría (2026-09-07)`:

```markdown
## D5 · Manager de usuarios v2 y reseteo de contraseña (decidido 2026-09-08)

- **Layout**: Opción A (tabla + ficha en panel lateral HTMX). B (página por usuario) y C
  (tarjetas) descartadas; mockups en el canvas enlazado desde la spec.
- **Correo**: Gmail con contraseña de aplicación, SMTP 587 STARTTLS con `smtplib` (sin
  dependencia nueva). Si molesta el remitente, cambiar `MONITOR_SMTP_*` a un proveedor
  transaccional no toca código.
- **HTTP sin dominio**: no hay dominio ni lo va a haber por un largo rato. El link de reseteo
  viaja en claro igual que hoy viaja la contraseña del login; mitigaciones: token de 256 bits
  hasheado, un solo uso, 60 min (invitación 72 h), consumirlo cierra las otras sesiones.
  Camino a HTTPS sin comprar dominio (Let's Encrypt sobre `129-80-148-166.sslip.io`): spike
  aparte, no verificado.
- **Autoservicio** "¿Olvidaste tu contraseña?" sí (respuesta neutra, rate-limit). **Alta por
  invitación** por defecto; contraseña inicial a mano como alternativa.
- Fases: F1 Manager + schema + login (esta rama) · F2 tokens + `/reset/{token}` + link copiable ·
  F3 mailer + `/forgot`.
```

- [ ] **Step 4: Gate completo**

Run: `pwsh scripts/check.ps1`
Expected: ruff limpio + pytest verde (skips esperados en Windows según la skill `/gate`).

- [ ] **Step 5: `/security-review`** sobre la rama (toca `deps_auth.py`, `routers/auth.py`, `routers/users_abm.py`, plantillas con handlers inline). Resolver hallazgos antes de seguir.

- [ ] **Step 6: Commit**

```bash
git add .claude/rules/auth.md docs/auth.md docs/decisiones.md
git commit -m "Docs: Manager v2 (rutas, is_active, users_service) y decisión D5"
```

- [ ] **Step 7: `/compound`** (cierre de fase): lección materializada = tests guardianes de `is_active` (login y sesión, con mutación), `_PUBLIC_PATHS` sin cambios verificados por el guard, regla en `.claude/rules/auth.md`; memoria `project_manager_usuarios_v2.md` actualizada a "F1 lista, pendiente merge/deploy"; anotar fuera de alcance (tokens, mail) para F2/F3. Después, `superpowers:finishing-a-development-branch` para decidir merge/PR; push y deploy sólo con OK de David.

---

## Self-review del plan

- **Cobertura de la spec (fila F1 de §9)**: columnas de `users` (T1), `base_public.html` + login restyle (T10), `is_active` en login y sesión (T4), Manager Opción A completo con canal "a mano" (T5-T7), datos (T6), permisos (T6), estado (T8), cerrar sesiones (T7), último acceso (T4 + T5), alta con contraseña y campos nuevos (T9), actividad derivada §2.3 (T3 + T5), política única §3.1 (T2), auditoría §7 (T6-T9), docs §9 (T11). `_PUBLIC_PATHS` no cambia en esta fase (lo verifica el guard existente).
- **Sin placeholders**: cada task trae el código y los tests completos; las rutas que las plantillas de T5 referencian se implementan en T6-T8 y el plan lo dice.
- **Consistencia de nombres**: `_users_page(request, db, *, status_code, selected_id, **ctx)` (T5) se usa igual en T6-T9; `password_invalida` (T2) en T7 y T9; `normalizar_email`/`email_invalido`/`_texto`/`_tabs_validas` (T3/T5) en T6 y T9; form fields `channel`/`password` (T7) coinciden con `submitReset` (T5); `activo` "0"/"1" (T8) coincide con los `<input type="hidden">` de la ficha (T5); `abrir_alta` (T9) coincide con `users.html` (T5).
