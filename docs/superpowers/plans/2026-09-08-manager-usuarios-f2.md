# Manager de usuarios v2 — Fase 2 · Plan de implementación (tokens, /reset, link copiable, invitación, login por email)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que un usuario pueda elegir su propia contraseña desde un link de un solo uso (reseteo o invitación) que el admin genera y copia desde el Manager, que el login acepte usuario **o** email, y que el admin pueda tipear la contraseña que quiera en el canal "a mano". Sin mail todavía (Fase 3).

**Architecture:** Tabla nueva `password_reset_tokens` (token aleatorio de 256 bits guardado como SHA-256, un uso, TTL 60 min / 72 h). Los helpers puros (`new_reset_token`, `hash_token`, centinela `SIN_PASSWORD_HASH`, `verify_password` robusto) van en `core/security.py`; las operaciones con sesión de DB (`issue/lookup/consume`, invitaciones vivas, tokens por usuario, URL absoluta) en un módulo nuevo `apps/web/reset_service.py` (deviación explícita de la spec, que los ponía todos en `core/security.py`: `core/` no debe depender del ORM ni de `request`). Las páginas públicas `GET/POST /reset/{token}` viven en `routers/auth.py` junto al login y extienden `base_public.html`. El Manager suma el canal `link` al reseteo y el alta por invitación; el estado "Invitación pendiente" y la actividad con tokens salen de `users_service` con los tokens que el router le pasa.

**Tech Stack:** FastAPI 0.141 + Jinja2 + HTMX 2.0.3 · SQLAlchemy 2 / SQLite (`catalog.db`) · passlib/bcrypt · `secrets`/`hashlib` stdlib · pytest + `TestClient`.

**Spec:** `docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md` (§2.2, §2.3, §3, §5.1 canal `link` y alta `invite`, §5.2 `/reset/{token}`, §5.3 login por usuario o email + banner `?reset=ok`, §5.4 rate-limit de `/reset`, §6 diálogo/canal a mano editable, §7, §8, fila F2 de §9).

## Global Constraints

- Intérprete siempre `py -3.12`. Otro worktree puede correr pytest a la vez: TODO pytest va con `MONITOR_TEST_DB_DIR=C:\Users\david\AppData\Local\Temp\monitor_pytest_f2` (dentro de `%TEMP%`, si no `conftest` lo ignora). `py -3.12 -m pytest <files> -q -p no:cacheprovider`.
- Schema forward-only: la tabla nueva la crea `create_all`; `users` no cambia. `CURRENT_SCHEMA_VERSION` no se toca.
- Tokens: `secrets.token_urlsafe(32)`; se persiste SÓLO `sha256(token).hexdigest()`; nunca se loguea el token ni la contraseña. TTL reset **60 min**, invitación **72 h**. Un solo uso; emitir uno nuevo invalida los vivos del usuario; usuario `is_active=0` → inválido; consumirlo sube `token_version`.
- Invitado sin contraseña = `hashed_password == "!"` (`core.security.SIN_PASSWORD_HASH`); `verify_password` contra un hash que no es bcrypt devuelve `False` **después** de verificar contra el hash dummy (mismo costo, sin excepción).
- `/reset/{token}` responde **200 con la misma página** ("Este link ya no sirve") para inexistente, vencido, usado o usuario deshabilitado; nunca distingue. La única ruta pública nueva es `/reset/{token}` (`_PUBLIC_PATHS` se actualiza **a sabiendas**; `/forgot` es de la Fase 3).
- Login: acepta usuario o email; `verify_password` corre siempre; mensaje único "Usuario o contraseña incorrectos"; la clave del rate-limit sigue siendo `(ip, valor tipeado en minúsculas)`.
- Política de contraseña: `core.security.password_invalida` (10 chars / 72 bytes) en TODOS los caminos (a mano, `/reset`).
- Auditoría: logger `monitor.audit`, campos por `_limpio`, `extra={"console": True}`. Acciones nuevas: `reset_link channel=link purpose=reset|invite by=… target=…`, `reset_consumed purpose=… target=…`, `invite_created by=… target=…`.
- Anti-XSS: username/nombre/email/notas sólo como texto o `data-*`; nunca dentro de un `on*=`. El link de reseteo se renderiza en un `<input readonly value="…">` autoescapado.
- Tests de auth real: `@pytest.mark.noauth`; los tests de `/reset` y de login son noauth. Fecha/hora: `datetime.now()` naive (ART por `apply_timezone`).
- Commits: uno por tarea en la rama `feat/manager-usuarios-f2` de un worktree fuera del proyecto (`~/.config/superpowers/worktrees/monitores/feat-manager-usuarios-f2`), mensajes en castellano, terminados en `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Nunca push ni deploy desde una tarea.
- `/security-review` antes de pushear (toca `routers/auth.py`, `deps_auth`, `users_abm.py`, rutas públicas).

---

### Task 0: Rama, worktree y baseline

- [ ] **Step 1:** `git worktree add "$HOME/.config/superpowers/worktrees/monitores/feat-manager-usuarios-f2" -b feat/manager-usuarios-f2 main` desde el repo principal; entrar con `EnterWorktree(path=...)`.
- [ ] **Step 2:** Copiar al worktree la spec actualizada y este plan (en `main` la spec tiene los dos deltas sin commitear): `docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md` y `docs/superpowers/plans/2026-09-08-manager-usuarios-f2.md`.
- [ ] **Step 3:** Baseline: `py -3.12 -m ruff check .` y `py -3.12 -m pytest tests/ -q -p no:cacheprovider` (con `MONITOR_TEST_DB_DIR`). Esperado: verde (3145 passed en `main` 8ddef69).
- [ ] **Step 4:** Commit: `git add docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md docs/superpowers/plans/2026-09-08-manager-usuarios-f2.md` → `git commit -m "Manager v2: spec con los deltas (login por email, clave manual editable) y plan de la Fase 2"`.

---

### Task 1: Login por usuario o email

**Files:**
- Modify: `apps/web/routers/auth.py` (import + lookup del usuario en `login`)
- Test: `tests/test_users_manager.py`

**Interfaces:**
- Consumes: `apps.web.users_service.normalizar_email` (Task 3 de F1).
- Produces: `POST /login` con `username="bob@ejemplo.com"` loguea a `bob`. Sin cambio de firma.

- [ ] **Step 1: Tests** (al final de `tests/test_users_manager.py`; la fixture `usuarios` ya crea a `bob` con `email="bob@ejemplo.com"`):

```python
# ── login por usuario o email ───────────────────────────────────────────────
@pytest.mark.noauth
def test_login_acepta_el_email_ademas_del_usuario(usuarios):
    with TestClient(app) as c:
        r = _login(c, "Bob@Ejemplo.com", "bobpass1234")      # mayúsculas: se normaliza
        assert r.status_code in (302, 303) and "access_token" in c.cookies
        assert c.get("/", follow_redirects=False).status_code == 200
    with SessionLocal() as s:
        assert s.query(UserORM).filter(UserORM.username == "bob").first().last_login_at is not None


@pytest.mark.noauth
def test_login_con_email_desconocido_o_clave_mal_es_indistinguible(usuarios):
    with TestClient(app) as c:
        r1 = _login(c, "nadie@ejemplo.com", "bobpass1234")
        r2 = _login(c, "bob@ejemplo.com", "clave-incorrecta")
        r3 = _login(c, "bob", "clave-incorrecta")
    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert r1.text == r2.text == r3.text
    assert "access_token" not in c.cookies
```

- [ ] **Step 2: RED** — `py -3.12 -m pytest tests/test_users_manager.py -q -p no:cacheprovider -k "acepta_el_email or indistinguible"` → el primero FALLA (200 sin cookie).

- [ ] **Step 3: Implementar.** En `apps/web/routers/auth.py` agregar el import `from apps.web.users_service import normalizar_email` y reemplazar la línea `user = db.query(UserORM).filter(UserORM.username == username).first()` de `login` por:

```python
    # El campo "Usuario" acepta usuario O email (spec §5.3). Primero el match exacto de
    # username (comportamiento histórico); si no hay, el email normalizado. El rate-limit
    # sigue clavado al valor tipeado y `verify_password` corre igual en los tres casos
    # (usuario, email, nada): sin oráculo de existencia.
    user = db.query(UserORM).filter(UserORM.username == username).first()
    if user is None:
        mail = normalizar_email(username)
        if mail and "@" in mail:
            user = db.query(UserORM).filter(UserORM.email == mail).first()
```

- [ ] **Step 4: GREEN** — mismo comando + `tests/test_auth.py tests/test_sec_audit_y_passwords.py`. Todo PASS.
- [ ] **Step 5: Commit** — `git add apps/web/routers/auth.py tests/test_users_manager.py` → `git commit -m "Login: el campo usuario acepta también el email"`.

---

### Task 2: Canal "a mano" con contraseña editable

**Files:**
- Modify: `apps/web/templates/pages/users.html` (función `resetPassword`)
- Test: `tests/test_users_manager.py`

**Produces:** el modal permite tipear la contraseña (precargada con una generada), botón "Generar otra", "Aplicar" deshabilitado con menos de 10 caracteres; el POST sigue siendo `channel=manual` + `password` a `/users/{id}/reset`.

- [ ] **Step 1: Test** (guardián del markup; el comportamiento JS se mira en el smoke):

```python
# ── canal a mano editable ───────────────────────────────────────────────────
@pytest.mark.noauth
def test_el_modal_de_clave_manual_permite_tipearla(usuarios):
    with TestClient(app) as c:
        _login_admin(c)
        html = c.get("/users").text
    i = html.index("function resetPassword(")
    modal = html[i:i + 4000]
    assert 'id="rp-pwd"' in modal and "readonly" not in modal.split('id="rp-pwd"')[1][:200], (
        "el campo de la contraseña tiene que ser editable")
    assert 'id="rp-gen"' in modal and "Generar otra" in modal
    assert "input.value.length < 10" in modal or "value.length < 10" in modal
```

- [ ] **Step 2: RED** — `-k modal_de_clave` FALLA (`readonly` presente, sin `rp-gen`).

- [ ] **Step 3: Implementar.** En `users.html`, reemplazar la función `resetPassword` completa por:

```javascript
// Reseteo a mano: modal propio (sin confirm() ni navigator.clipboard, que sobre HTTP
// no existe). La contraseña viene GENERADA pero es EDITABLE: el admin puede tipear la
// que quiera (mínimo 10 caracteres, igual que el servidor). El copiado usa window.mrCopy
// (base.html), que cae a execCommand sin HTTPS.
function resetPassword(userId, username) {
  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.innerHTML = `
    <div class="modal-card" style="max-width:480px" role="dialog" aria-modal="true" aria-labelledby="rp-title">
      <div class="modal-head"><b id="rp-title">Definir contraseña a mano</b><span class="mut">${escapeHtml(username)}</span></div>
      <div class="modal-body">
        <p style="margin:0 0 4px">Nueva contraseña para <b>${escapeHtml(username)}</b> (podés tipear la tuya):</p>
        <div style="display:flex; gap:8px; margin:10px 0">
          <input id="rp-pwd" type="text" value="${escapeHtml(generateSecurePassword())}" minlength="10" maxlength="72"
                 autocomplete="off" spellcheck="false"
                 style="flex:1; font-family:var(--font-mono); font-size:15px; letter-spacing:.5px; padding:9px 10px">
          <button type="button" id="rp-gen" class="btn sec" title="Generar otra contraseña segura">Generar otra</button>
          <button type="button" id="rp-copy" class="btn sec">Copiar</button>
        </div>
        <p class="help" id="rp-help" style="margin:0">Mínimo 10 caracteres. Guardala <b>antes</b> de confirmar: una vez
           aplicada no se puede volver a ver (se almacena hasheada con bcrypt). Al aplicarla se cierran las
           sesiones abiertas del usuario.</p>
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
  const btnOk = ov.querySelector("#rp-ok");
  const btnCopy = ov.querySelector("#rp-copy");
  const validar = () => {
    const corta = input.value.length < 10;
    btnOk.disabled = corta;
    btnOk.style.opacity = corta ? ".5" : "1";
  };
  input.addEventListener("input", validar);
  validar();
  input.focus(); input.select();
  ov.querySelector("#rp-gen").addEventListener("click", () => {
    input.value = generateSecurePassword(); validar(); input.focus(); input.select();
  });
  btnCopy.addEventListener("click", () => {
    window.mrCopy(input.value, (ok) => {
      btnCopy.textContent = ok ? "¡Copiado!" : "Copiá con Ctrl+C";
      input.focus(); input.select();
      setTimeout(() => { btnCopy.textContent = "Copiar"; }, 2500);
    });
  });
  ov.querySelector("#rp-cancel").addEventListener("click", close);
  btnOk.addEventListener("click", () => {
    if (input.value.length < 10) return;
    const pwd = input.value; close(); submitReset(userId, pwd);
  });
}
```

- [ ] **Step 4: GREEN** — `-k modal_de_clave` + `tests/test_aud_D1_seguridad_web.py` (el username sigue fuera de los handlers). PASS.
- [ ] **Step 5: Commit** — `git commit -m "Manager v2: la contraseña a mano se puede tipear (precargada, con Generar otra)"`.

---

### Task 3: Tokens de reseteo — ORM, helpers puros y `reset_service`

**Files:**
- Modify: `core/infrastructure/db/models.py` (clase `PasswordResetTokenORM`)
- Modify: `core/security.py` (`SIN_PASSWORD_HASH`, `new_reset_token`, `hash_token`, `verify_password` robusto)
- Create: `apps/web/reset_service.py`
- Test: `tests/test_users_manager.py`

**Interfaces (Produces):**
- `core.security.SIN_PASSWORD_HASH = "!"`; `new_reset_token() -> str`; `hash_token(token: str) -> str` (sha256 hex); `verify_password(plain, hashed) -> bool` nunca lanza: hash que no empieza con `$2` → verifica contra el dummy y devuelve `False`.
- `PasswordResetTokenORM` (tabla `password_reset_tokens`): `id, user_id (FK users.id, index), token_hash (unique), purpose ("reset"|"invite"), channel ("link"|"mail"|"self"), created_at, created_by (str|None), expires_at, used_at (None = vivo)`.
- `apps/web/reset_service.py`: `RESET_TTL = timedelta(minutes=60)`, `INVITE_TTL = timedelta(hours=72)`; `issue_reset_token(db, user, *, purpose, channel, by) -> str` (invalida los vivos del usuario, limpieza perezosa >30 días, devuelve el token en claro); `lookup_reset_token(db, token) -> tuple[UserORM, PasswordResetTokenORM] | None`; `consume_reset_token(db, token, new_password) -> UserORM | None`; `reset_link(request, token) -> str` (`settings.public_url` o `request.base_url`); `invitaciones_vivas(db) -> dict[int, PasswordResetTokenORM]` (invite vivo por `user_id`); `tokens_de(db, user) -> list[PasswordResetTokenORM]` (todos, del más nuevo al más viejo).
- `config/settings.py`: campo `public_url: str = ""` (env `MONITOR_PUBLIC_URL`, p. ej. `http://129.80.148.166`), comentado.

- [ ] **Step 1: Tests**

```python
# ── tokens de reseteo ───────────────────────────────────────────────────────
from datetime import timedelta


def test_verify_password_no_explota_con_el_centinela_sin_clave():
    from core.security import SIN_PASSWORD_HASH, verify_password
    assert SIN_PASSWORD_HASH == "!"
    assert verify_password("cualquier-cosa", SIN_PASSWORD_HASH) is False
    assert verify_password("x", "") is False
    assert verify_password("x", "hash-que-no-es-bcrypt") is False


def test_token_nuevo_es_aleatorio_y_se_guarda_hasheado():
    from core.security import hash_token, new_reset_token
    a, b = new_reset_token(), new_reset_token()
    assert a != b and len(a) >= 40
    assert hash_token(a) != a and len(hash_token(a)) == 64 and hash_token(a) == hash_token(a)


@pytest.mark.noauth
def test_issue_lookup_consume_y_un_solo_uso(usuarios):
    from apps.web import reset_service as rs
    from core.infrastructure.db.models import PasswordResetTokenORM
    with SessionLocal() as s:
        bob = s.query(UserORM).filter(UserORM.username == "bob").first()
        t1 = rs.issue_reset_token(s, bob, purpose="reset", channel="link", by="admin")
        t2 = rs.issue_reset_token(s, bob, purpose="reset", channel="link", by="admin")
        assert rs.lookup_reset_token(s, t1) is None, "emitir uno nuevo invalida el anterior"
        user, row = rs.lookup_reset_token(s, t2)
        assert user.id == bob.id and row.purpose == "reset" and row.channel == "link" and row.created_by == "admin"
        assert timedelta(minutes=59) < (row.expires_at - row.created_at) <= timedelta(minutes=60)
        hash_antes, ver_antes = bob.hashed_password, bob.token_version or 0
        assert rs.consume_reset_token(s, t2, "clave-nueva-123").id == bob.id
        s.refresh(bob)
        assert bob.hashed_password != hash_antes and bob.token_version == ver_antes + 1
        assert bob.password_changed_at is not None
        assert rs.lookup_reset_token(s, t2) is None, "un token consumido no vuelve a servir"
        assert rs.consume_reset_token(s, t2, "otra-clave-1234") is None
        assert s.query(PasswordResetTokenORM).filter(PasswordResetTokenORM.token_hash == t2).count() == 0, \
            "el token en claro NUNCA se persiste"
        assert rs.lookup_reset_token(s, "token-inventado") is None


@pytest.mark.noauth
def test_token_vencido_o_de_usuario_deshabilitado_no_vale(usuarios):
    from apps.web import reset_service as rs
    from core.infrastructure.db.models import PasswordResetTokenORM
    with SessionLocal() as s:
        bob = s.query(UserORM).filter(UserORM.username == "bob").first()
        t = rs.issue_reset_token(s, bob, purpose="invite", channel="link", by="admin")
        row = s.query(PasswordResetTokenORM).filter(PasswordResetTokenORM.used_at.is_(None)).one()
        assert timedelta(hours=71) < (row.expires_at - row.created_at) <= timedelta(hours=72)
        row.expires_at = datetime.now() - timedelta(seconds=1)
        s.commit()
        assert rs.lookup_reset_token(s, t) is None
        t2 = rs.issue_reset_token(s, bob, purpose="reset", channel="link", by="admin")
        bob.is_active = False
        s.commit()
        assert rs.lookup_reset_token(s, t2) is None


@pytest.mark.noauth
def test_invitaciones_vivas_tokens_de_y_link(usuarios):
    from apps.web import reset_service as rs
    with SessionLocal() as s:
        bob = s.query(UserORM).filter(UserORM.username == "bob").first()
        assert rs.invitaciones_vivas(s) == {}
        t = rs.issue_reset_token(s, bob, purpose="invite", channel="link", by="admin")
        vivas = rs.invitaciones_vivas(s)
        assert list(vivas) == [bob.id] and vivas[bob.id].purpose == "invite"
        rs.issue_reset_token(s, bob, purpose="reset", channel="link", by="admin")
        assert rs.invitaciones_vivas(s) == {}, "el reset nuevo invalidó la invitación"
        assert [x.purpose for x in rs.tokens_de(s, bob)] == ["reset", "invite"]

    class _Req:
        base_url = "http://testserver/"
    from config.settings import settings
    viejo = settings.public_url
    try:
        settings.public_url = ""
        assert rs.reset_link(_Req(), "abc") == "http://testserver/reset/abc"
        settings.public_url = "http://129.80.148.166/"
        assert rs.reset_link(_Req(), "abc") == "http://129.80.148.166/reset/abc"
    finally:
        settings.public_url = viejo
```

- [ ] **Step 2: RED** — `-k "centinela or aleatorio or issue_lookup or vencido_o or invitaciones_vivas"` → FAIL (imports).

- [ ] **Step 3: `core/security.py`** — agregar arriba `import hashlib`, `import secrets` y reemplazar `verify_password` + agregar helpers:

```python
# Centinela "sin contraseña": el usuario invitado existe pero todavía no eligió clave.
# NO es un hash bcrypt: `verify_password` lo rechaza sin lanzar y con el mismo costo.
SIN_PASSWORD_HASH = "!"
_DUMMY_HASH: Optional[str] = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = pwd_context.hash("x")
    return _DUMMY_HASH


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """False para cualquier hash que no sea bcrypt (centinela `!`, vacío, basura), pero
    verificando igual contra un hash dummy para no delatar por tiempo que la cuenta no
    tiene clave. passlib lanzaría ValueError con un hash irreconocible."""
    if not hashed_password or not hashed_password.startswith("$2"):
        pwd_context.verify(plain_password, _dummy_hash())
        return False
    return pwd_context.verify(plain_password, hashed_password)


def new_reset_token() -> str:
    """Token de reseteo/invitación: 256 bits, URL-safe. Se persiste SÓLO su hash."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: `core/infrastructure/db/models.py`** — después de `UserORM`:

```python
class PasswordResetTokenORM(Base):
    """Token de reseteo de contraseña o de invitación (spec 2026-09-08 §2.2). Se guarda
    el SHA-256 del token, nunca el token: quien lea la DB no puede usarlo. Un solo uso
    (`used_at`), vence (`expires_at`), y emitir uno nuevo invalida los vivos del usuario.
    Tabla nueva: la crea `create_all`; no hay migración de datos."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    purpose: Mapped[str] = mapped_column(String)      # "reset" | "invite"
    channel: Mapped[str] = mapped_column(String)      # "link" | "mail" | "self"
    created_at: Mapped[datetime] = mapped_column(DateTime)
    created_by: Mapped[Optional[str]] = mapped_column(String, default=None)   # username del admin; None = autoservicio
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
```

- [ ] **Step 5: `config/settings.py`** — debajo de `trusted_proxy_ips`:

```python
    # Base ABSOLUTA de los links que la app manda o muestra (reseteo de contraseña,
    # invitaciones): p. ej. "http://129.80.148.166". Vacío = se arma con `request.base_url`
    # (correcto detrás del nginx local, que reenvía Host). Override: MONITOR_PUBLIC_URL.
    public_url: str = ""
```

- [ ] **Step 6: `apps/web/reset_service.py`** (nuevo):

```python
"""Tokens de reseteo de contraseña e invitación: emisión, validación y consumo (spec
2026-09-08 §3). Acá vive lo que necesita sesión de DB o `request`; los helpers puros
(`new_reset_token`, `hash_token`, `SIN_PASSWORD_HASH`) están en `core/security.py`.

Contrato: token aleatorio de 256 bits, persistido SÓLO como SHA-256; un solo uso; vence
(reset 60 min, invitación 72 h); emitir uno nuevo invalida los vivos del mismo usuario;
un usuario deshabilitado no puede consumirlo; consumirlo sube `token_version` (cierra las
demás sesiones). Nunca se loguea el token."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from config.settings import settings
from core.infrastructure.db.models import PasswordResetTokenORM, UserORM
from core.security import get_password_hash, hash_token, new_reset_token

RESET_TTL = timedelta(minutes=60)
INVITE_TTL = timedelta(hours=72)
_PURGA = timedelta(days=30)   # limpieza perezosa de vencidos viejos, sin loop nuevo


def issue_reset_token(db: Session, user: UserORM, *, purpose: str, channel: str,
                      by: Optional[str]) -> str:
    """Emite un token nuevo para `user` e invalida los que tuviera vivos. Devuelve el token
    en claro UNA vez: el llamador lo muestra o lo manda y no lo guarda."""
    if purpose not in ("reset", "invite"):
        raise ValueError(f"purpose inválido: {purpose!r}")
    now = datetime.now()
    db.query(PasswordResetTokenORM).filter(
        PasswordResetTokenORM.user_id == user.id,
        PasswordResetTokenORM.used_at.is_(None),
    ).update({"used_at": now}, synchronize_session=False)
    db.query(PasswordResetTokenORM).filter(
        PasswordResetTokenORM.expires_at < now - _PURGA
    ).delete(synchronize_session=False)
    token = new_reset_token()
    db.add(PasswordResetTokenORM(
        user_id=user.id, token_hash=hash_token(token), purpose=purpose, channel=channel,
        created_at=now, created_by=by,
        expires_at=now + (INVITE_TTL if purpose == "invite" else RESET_TTL),
    ))
    db.commit()
    return token


def lookup_reset_token(db: Session, token: str):
    """(usuario, fila) si el token está vivo, no vencido y el usuario activo; si no, None,
    sin distinguir el motivo (la página pública tampoco lo distingue)."""
    if not token or len(token) > 128:
        return None
    row = db.query(PasswordResetTokenORM).filter(
        PasswordResetTokenORM.token_hash == hash_token(token)).first()
    if row is None or row.used_at is not None or row.expires_at <= datetime.now():
        return None
    user = db.get(UserORM, row.user_id)
    if user is None or not user.is_active:
        return None
    return user, row


def consume_reset_token(db: Session, token: str, new_password: str) -> Optional[UserORM]:
    """Cambia la contraseña, marca el token usado y cierra las otras sesiones. La política
    de la contraseña la valida el llamador (`password_invalida`) ANTES de llamar acá."""
    found = lookup_reset_token(db, token)
    if found is None:
        return None
    user, row = found
    now = datetime.now()
    user.hashed_password = get_password_hash(new_password)
    user.password_changed_at = now
    user.token_version = (user.token_version or 0) + 1
    row.used_at = now
    db.commit()
    return user


def reset_link(request, token: str) -> str:
    base = (settings.public_url or str(request.base_url)).rstrip("/")
    return f"{base}/reset/{token}"


def invitaciones_vivas(db: Session) -> dict:
    """{user_id: token} de las invitaciones vivas (para el estado de la tabla del Manager)."""
    now = datetime.now()
    rows = db.query(PasswordResetTokenORM).filter(
        PasswordResetTokenORM.purpose == "invite",
        PasswordResetTokenORM.used_at.is_(None),
        PasswordResetTokenORM.expires_at > now,
    ).all()
    return {r.user_id: r for r in rows}


def tokens_de(db: Session, user: UserORM) -> list:
    return db.query(PasswordResetTokenORM).filter(
        PasswordResetTokenORM.user_id == user.id
    ).order_by(PasswordResetTokenORM.created_at.desc(), PasswordResetTokenORM.id.desc()).all()
```

- [ ] **Step 7: GREEN** — los 5 tests + `tests/test_auth.py tests/test_sec_audit_y_passwords.py tests/test_users_manager.py`. Ruff limpio.
- [ ] **Step 8: Commit** — `git commit -m "Manager v2: tokens de reseteo/invitación (tabla, helpers, reset_service) y verify_password robusto"`.

---

### Task 4: Página pública `/reset/{token}` + banner en el login

**Files:**
- Modify: `apps/web/routers/auth.py` (`login_page` con `reset`, rutas `GET/POST /reset/{token}`, limiter de `/reset`)
- Create: `apps/web/templates/pages/reset.html`, `apps/web/templates/pages/reset_invalido.html`
- Modify: `apps/web/templates/pages/login.html` (banner `reset_ok`)
- Modify: `tests/test_aud_G_tests_route_auth.py:31` (`_PUBLIC_PATHS`)
- Test: `tests/test_users_manager.py`

**Produces:** `GET /reset/{token}` → 200 formulario si el token vale, 200 "Este link ya no sirve" si no. `POST /reset/{token}` (form `password`, `password2`) → 400 con el formulario si no coinciden o no cumplen la política (el token sigue vivo); 200 "ya no sirve" si el token no vale; éxito → **303 `/login?reset=ok`**. Rate-limit del POST: 10 por IP / 15 min → 429. `GET /login?reset=ok` muestra el banner verde "Tu contraseña se actualizó. Ingresá con la nueva.".

- [ ] **Step 1: Tests**

```python
# ── /reset/{token} ──────────────────────────────────────────────────────────
def _token_de_bob(purpose="reset"):
    from apps.web import reset_service as rs
    with SessionLocal() as s:
        bob = s.query(UserORM).filter(UserORM.username == "bob").first()
        return rs.issue_reset_token(s, bob, purpose=purpose, channel="link", by="admin")


@pytest.mark.noauth
def test_reset_get_muestra_el_formulario_solo_con_token_valido(usuarios):
    t = _token_de_bob()
    with TestClient(app) as c:
        ok = c.get(f"/reset/{t}")
        malo = c.get("/reset/token-inventado")
    assert ok.status_code == 200 and 'name="password2"' in ok.text and "Bob" in ok.text and "bob" in ok.text
    assert "MONITOR · Renta Fija AR" in ok.text and "hx-get" not in ok.text
    assert malo.status_code == 200 and "ya no sirve" in malo.text and 'name="password"' not in malo.text


@pytest.mark.noauth
def test_reset_post_cambia_la_clave_cierra_sesiones_y_vuelve_al_login(usuarios):
    t = _token_de_bob()
    with TestClient(app) as bob_c, TestClient(app) as anon:
        assert _login(bob_c, "bob", "bobpass1234").status_code in (302, 303)
        r = anon.post(f"/reset/{t}", data={"password": "mi-clave-nueva-1", "password2": "mi-clave-nueva-1"},
                      follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?reset=ok"
        assert bob_c.get("/", follow_redirects=False).status_code == 302, "las otras sesiones tienen que morir"
        assert _login(anon, "bob", "mi-clave-nueva-1").status_code in (302, 303)
        # el mismo link, otra vez: ya no sirve (un solo uso). Mutación: sacar el chequeo de
        # used_at en lookup_reset_token pone esto en rojo.
        r2 = anon.post(f"/reset/{t}", data={"password": "otra-clave-9999", "password2": "otra-clave-9999"},
                       follow_redirects=False)
        assert r2.status_code == 200 and "ya no sirve" in r2.text
        banner = anon.get("/login?reset=ok")
        assert "Tu contraseña se actualizó" in banner.text
        assert "Tu contraseña se actualizó" not in anon.get("/login").text


@pytest.mark.noauth
def test_reset_post_rechaza_clave_corta_o_distinta_y_el_token_sigue_vivo(usuarios):
    t = _token_de_bob()
    with TestClient(app) as c:
        r1 = c.post(f"/reset/{t}", data={"password": "corta", "password2": "corta"})
        r2 = c.post(f"/reset/{t}", data={"password": "clave-larga-ok-1", "password2": "clave-larga-ok-2"})
        assert r1.status_code == 400 and 'name="password2"' in r1.text and "al menos 10" in r1.text
        assert r2.status_code == 400 and "no coinciden" in r2.text
        assert c.get(f"/reset/{t}").status_code == 200 and 'name="password2"' in c.get(f"/reset/{t}").text
    with SessionLocal() as s:
        assert s.query(UserORM).filter(UserORM.username == "bob").first().password_changed_at is None


@pytest.mark.noauth
def test_reset_de_usuario_deshabilitado_o_vencido_es_la_misma_pagina(usuarios):
    from datetime import timedelta
    from core.infrastructure.db.models import PasswordResetTokenORM
    t = _token_de_bob()
    _set_bob(is_active=False)
    with TestClient(app) as c:
        r_off = c.get(f"/reset/{t}")
    _set_bob(is_active=True)
    with SessionLocal() as s:
        s.query(PasswordResetTokenORM).update({"expires_at": datetime.now() - timedelta(seconds=1)})
        s.commit()
    with TestClient(app) as c:
        r_venc = c.get(f"/reset/{t}")
        r_nada = c.get("/reset/xyz")
    assert r_off.status_code == r_venc.status_code == r_nada.status_code == 200
    assert r_off.text == r_venc.text == r_nada.text


@pytest.mark.noauth
def test_reset_post_tiene_rate_limit_por_ip(usuarios):
    from apps.web.routers import auth as auth_router
    auth_router._reset_attempts.clear()
    with TestClient(app) as c:
        codigos = [c.post("/reset/token-falso", data={"password": "x" * 12, "password2": "x" * 12}).status_code
                   for _ in range(11)]
    assert codigos[:10] == [200] * 10 and codigos[10] == 429
    auth_router._reset_attempts.clear()
```

En `tests/test_aud_G_tests_route_auth.py:31`: `_PUBLIC_PATHS = {"/login", "/logout", "/api/health", "/reset/{token}"}` con un comentario: `# /reset/{token}: página pública de nueva contraseña (spec 2026-09-08 §5.2); el token es la credencial.`

- [ ] **Step 2: RED** — `-k "reset_get or reset_post or reset_de_usuario"` → 404s.

- [ ] **Step 3: Router.** En `apps/web/routers/auth.py`: imports `from apps.web import reset_service`, `from core.security import password_invalida` (sumarla al import existente). Debajo de `_login_attempts`:

```python
# Rate-limit de POST /reset/{token}: por IP, 10 / 15 min. El token tiene 256 bits (no se
# adivina), esto sólo frena martillar la ruta.
_MAX_RESET_ATTEMPTS = 10
_RESET_WINDOW_SEC = 900
_reset_attempts: dict = defaultdict(list)


def _rate_limited(bucket: dict, key, max_attempts: int, window: float) -> bool:
    """True si `key` ya agotó `max_attempts` en `window` segundos; registra el intento."""
    now = time.time()
    recent = [t for t in bucket[key] if now - t < window]
    if len(recent) >= max_attempts:
        bucket[key] = recent
        return True
    recent.append(now)
    bucket[key] = recent
    if len(bucket) > _MAX_TRACKED_KEYS:
        for k in sorted(bucket, key=lambda k: bucket[k][-1])[:len(bucket) - _MAX_TRACKED_KEYS]:
            bucket.pop(k, None)
    return False
```

Reemplazar `login_page`:

```python
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, reset: str = ""):
    # `?reset=ok` lo pone POST /reset/{token} al terminar: banner "ingresá con la nueva".
    return _TEMPLATES.TemplateResponse(request, "pages/login.html",
                                       {"error": None, "reset_ok": reset == "ok"})
```

Agregar al final del módulo (antes de `logout` o después, da igual):

```python
# ── Nueva contraseña por link (reseteo o invitación) ──────────────────────────
# Pública a sabiendas (tests/test_aud_G_tests_route_auth.py::_PUBLIC_PATHS): la
# credencial es el token. Toda invalidez (inexistente, vencido, usado, usuario
# deshabilitado) responde la MISMA página con 200: sin oráculo.
_INVALIDO = "pages/reset_invalido.html"


def _form_reset(request, user, token, error=None, status_code=200):
    return _TEMPLATES.TemplateResponse(
        request, "pages/reset.html",
        {"nombre": (user.full_name or user.username).split()[0], "username": user.username,
         "token": token, "error": error}, status_code=status_code)


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_page(request: Request, token: str, db: Session = Depends(get_db)):
    found = reset_service.lookup_reset_token(db, token)
    if found is None:
        return _TEMPLATES.TemplateResponse(request, _INVALIDO, {})
    user, _row = found
    return _form_reset(request, user, token)


@router.post("/reset/{token}", response_class=HTMLResponse)
def reset_submit(request: Request, token: str, password: str = Form(""),
                 password2: str = Form(""), db: Session = Depends(get_db)):
    ip = _client_ip(request)
    if _rate_limited(_reset_attempts, ip, _MAX_RESET_ATTEMPTS, _RESET_WINDOW_SEC):
        _audit.info("auth reset=ratelimited ip=%s", _limpio(ip), extra={"console": True})
        return _TEMPLATES.TemplateResponse(request, _INVALIDO, {"ratelimited": True}, status_code=429)
    found = reset_service.lookup_reset_token(db, token)
    if found is None:
        return _TEMPLATES.TemplateResponse(request, _INVALIDO, {})
    user, row = found
    if password != password2:
        return _form_reset(request, user, token, error="Las dos contraseñas no coinciden.", status_code=400)
    invalida = password_invalida(password)
    if invalida:
        return _form_reset(request, user, token, error=invalida, status_code=400)
    reset_service.consume_reset_token(db, token, password)
    _audit.info("auth reset_consumed purpose=%s target=%s ip=%s", _limpio(row.purpose),
                _limpio(user.username), _limpio(ip), extra={"console": True})
    return RedirectResponse(url="/login?reset=ok", status_code=status.HTTP_303_SEE_OTHER)
```

- [ ] **Step 4: Templates.** `pages/reset.html`:

```html
{% extends "base_public.html" %}
{% block title %}Nueva contraseña - Monitor Renta Fija AR{% endblock %}
{% block header_meta %}Nueva contraseña{% endblock %}
{% block content %}
<div class="pub-card">
  <h2>Hola, {{ nombre }}</h2>
  <p class="sub">Elegí la nueva contraseña de tu usuario <b>{{ username }}</b>.</p>
  {% if error %}<div class="msg error">{{ error }}</div>{% endif %}
  <form method="POST" action="/reset/{{ token }}" autocomplete="off">
    <label class="fld"><span>Nueva contraseña</span>
      <input type="password" name="password" required minlength="10" maxlength="72" autofocus autocomplete="new-password"></label>
    <label class="fld"><span>Repetir contraseña</span>
      <input type="password" name="password2" required minlength="10" maxlength="72" autocomplete="new-password"></label>
    <p class="sub" style="margin-top: -6px;">Al menos 10 caracteres. Al guardar se cierran las otras sesiones abiertas de tu usuario.</p>
    <button type="submit">Guardar contraseña</button>
  </form>
</div>
{% endblock %}
```

`pages/reset_invalido.html`:

```html
{% extends "base_public.html" %}
{% block title %}Link vencido - Monitor Renta Fija AR{% endblock %}
{% block header_meta %}Nueva contraseña{% endblock %}
{% block content %}
<div class="pub-card">
  <h2>Este link ya no sirve</h2>
  {% if ratelimited %}
  <p class="sub">Demasiados intentos desde tu conexión. Esperá unos minutos y volvé a abrir el link.</p>
  {% else %}
  <p class="sub">Los links vencen a los 60 minutos y se usan una sola vez. Si ya cambiaste la contraseña, ingresá con la nueva; si no, pedile a tu administrador un link nuevo.</p>
  {% endif %}
  <div class="links"><a href="/login">Volver al ingreso</a></div>
</div>
{% endblock %}
```

`pages/login.html`: después de `{% if error %}…{% endif %}` agregar `{% if reset_ok %}<div class="msg success">Tu contraseña se actualizó. Ingresá con la nueva.</div>{% endif %}`.

- [ ] **Step 5: GREEN** — los 5 tests + `tests/test_aud_G_tests_route_auth.py tests/test_auth.py tests/test_sec_csrf_y_headers.py`. Ruff limpio.
- [ ] **Step 6: Mutación** — comentar `or row.used_at is not None` en `lookup_reset_token` → `test_reset_post_cambia_la_clave_cierra_sesiones_y_vuelve_al_login` ROJO (segundo uso pasa); restaurar. Comentar `if user is None or not user.is_active` → `test_reset_de_usuario_deshabilitado_o_vencido_es_la_misma_pagina` ROJO; restaurar.
- [ ] **Step 7: Commit** — `git add apps/web/routers/auth.py apps/web/templates/pages/reset.html apps/web/templates/pages/reset_invalido.html apps/web/templates/pages/login.html tests/test_aud_G_tests_route_auth.py tests/test_users_manager.py` → `git commit -m "Página pública /reset/{token}: el usuario elige su contraseña con un link de un solo uso"`.

---

### Task 5: Canal `link` en el Manager + actividad con tokens

**Files:**
- Modify: `apps/web/routers/users_abm.py` (`reset_password` canal `link`, `_ctx_ficha(db, u)`, `_users_page` con invitaciones)
- Modify: `apps/web/users_service.py` (`estado_usuario`, `vista_usuario`, `actividad_reciente` con tokens; `fmt_restante`)
- Modify: `apps/web/templates/fragments/user_ficha.html` (botón "Generar link para copiar"), `apps/web/templates/pages/users.html` (caja del link generado; badge de estado)
- Test: `tests/test_users_manager.py`

**Interfaces:**
- `users_service.fmt_restante(hasta: datetime, ahora: datetime|None = None) -> str` → `"vence en 2 d"`, `"vence en 5 h"`, `"vence en 45 min"`, `"vencida"`.
- `users_service.estado_usuario(u, invitacion=None) -> str` → `"deshabilitado"` | `"invitado"` (si `u.hashed_password == SIN_PASSWORD_HASH`) | `"activo"`.
- `users_service.vista_usuario(u, hoy=None, invitacion=None, ahora=None)` suma `estado`, `invitacion_txt` (`"Invitación · vence en 2 d"` / `"Invitación vencida"` / `""`).
- `users_service.actividad_reciente(u, hoy=None, tokens=())` suma eventos por token: creado → `"Link de reseteo generado"`/`"Invitación generada"` (detalle `por <created_by>` o `autoservicio`, `· canal <channel>`); usado → `"Contraseña elegida desde el link"`/`"Invitación aceptada"`. Si un token fue usado a ≤ 2 s de `password_changed_at`, NO se agrega el evento genérico "Contraseña cambiada · por un administrador" (evita el duplicado).
- Router: `POST /users/{id}/reset` con `channel=link` → emite token (`purpose="reset"`, `channel="link"`, `by=admin.username`), audita `users action=reset_link channel=link purpose=reset by=… target=…`, responde `_users_page(..., selected_id, link_reset=<url>, link_para=<username>, link_vence="60 minutos", success=...)`. `channel=mail` sigue dando 400 "no disponible todavía" (Fase 3). `_users_page` calcula `invitaciones = reset_service.invitaciones_vivas(db)` y pasa `vista_usuario(u, invitacion=invitaciones.get(u.id))`; `_ctx_ficha(db, u)` pasa `tokens_de(db, u)` a `actividad_reciente` y la invitación viva a `vista_usuario`.

- [ ] **Step 1: Tests**

```python
# ── canal link + actividad ──────────────────────────────────────────────────
def test_fmt_restante_y_estado_invitado():
    from datetime import timedelta
    from apps.web.users_service import estado_usuario, fmt_restante
    from core.security import SIN_PASSWORD_HASH
    ahora = datetime(2026, 9, 8, 10, 0)
    assert fmt_restante(ahora + timedelta(days=2, hours=3), ahora) == "vence en 2 d"
    assert fmt_restante(ahora + timedelta(hours=5, minutes=10), ahora) == "vence en 5 h"
    assert fmt_restante(ahora + timedelta(minutes=45), ahora) == "vence en 45 min"
    assert fmt_restante(ahora - timedelta(seconds=1), ahora) == "vencida"
    assert estado_usuario(_u(hashed_password=SIN_PASSWORD_HASH)) == "invitado"
    assert estado_usuario(_u(hashed_password=SIN_PASSWORD_HASH, is_active=False)) == "deshabilitado"


@pytest.mark.noauth
def test_canal_link_genera_un_link_que_funciona_y_queda_en_la_actividad(usuarios):
    import re
    bob = _bob_id()
    with TestClient(app) as admin_c, TestClient(app) as anon:
        _login_admin(admin_c)
        r = admin_c.post(f"/users/{bob}/reset", data={"channel": "link"})
        assert r.status_code == 200
        m = re.search(r'value="(http://testserver/reset/[A-Za-z0-9_\-]+)"', r.text)
        assert m, "el link tiene que mostrarse una vez en un input readonly"
        link = m.group(1)
        assert "Link de reseteo generado" in r.text and "60 minutos" in r.text
        assert anon.get(link.replace("http://testserver", "")).status_code == 200
        r2 = anon.post(link.replace("http://testserver", ""),
                       data={"password": "elegida-por-bob-1", "password2": "elegida-por-bob-1"},
                       follow_redirects=False)
        assert r2.status_code == 303
        ficha = admin_c.get(f"/users/{bob}/ficha").text
        assert "Contraseña elegida desde el link" in ficha
        assert "por un administrador" not in ficha, "el evento genérico duplicaría el del token"
        assert admin_c.post(f"/users/{bob}/reset", data={"channel": "mail"}).status_code == 400
        assert admin_c.post("/users/999999/reset", data={"channel": "link"}).status_code == 404
```

- [ ] **Step 2: RED** — `-k "fmt_restante or canal_link"`.

- [ ] **Step 3: `users_service.py`.** Agregar `from core.security import SIN_PASSWORD_HASH` y reemplazar `estado_usuario`, `vista_usuario`, `actividad_reciente`; agregar `fmt_restante`:

```python
def fmt_restante(hasta: datetime, ahora: Optional[datetime] = None) -> str:
    """'vence en 2 d' · 'vence en 5 h' · 'vence en 45 min' · 'vencida'."""
    ahora = ahora or datetime.now()
    seg = (hasta - ahora).total_seconds()
    if seg <= 0:
        return "vencida"
    if seg >= 86400:
        return f"vence en {int(seg // 86400)} d"
    if seg >= 3600:
        return f"vence en {int(seg // 3600)} h"
    return f"vence en {max(1, int(seg // 60))} min"


def estado_usuario(u: UserORM, invitacion=None) -> str:
    if not u.is_active:
        return "deshabilitado"
    if u.hashed_password == SIN_PASSWORD_HASH:
        return "invitado"          # creado por invitación, todavía sin contraseña
    return "activo"


def vista_usuario(u: UserORM, hoy: Optional[date] = None, invitacion=None,
                  ahora: Optional[datetime] = None) -> dict:
    """Lo que la tabla y la cabecera de la ficha muestran de un usuario. `invitacion` es
    el token de invitación VIVO del usuario (o None), que el router saca de la DB."""
    estado = estado_usuario(u, invitacion)
    if estado == "invitado":
        inv_txt = f"Invitación · {fmt_restante(invitacion.expires_at, ahora)}" if invitacion else "Invitación vencida"
    else:
        inv_txt = ""
    return {
        "u": u,
        "iniciales": iniciales(u),
        "estado": estado,
        "invitacion_txt": inv_txt,
        "sin_email": not u.email,
        "ultimo_acceso": fmt_momento(u.last_login_at, hoy),
        "pwd_cambiada": fmt_momento(u.password_changed_at, hoy),
        "alta": fmt_momento(u.created_at, hoy),
        "tabs": [] if u.is_admin else [t for t in (u.allowed_tabs or []) if t != "*"],
    }


_QUE_CREADO = {"reset": "Link de reseteo generado", "invite": "Invitación generada"}
_QUE_USADO = {"reset": "Contraseña elegida desde el link", "invite": "Invitación aceptada"}


def actividad_reciente(u: UserORM, hoy: Optional[date] = None, tokens=()) -> list[dict]:
    """Eventos DERIVADOS de las columnas y de los tokens (sin tabla de eventos), del más
    nuevo al más viejo."""
    ev: list[dict] = []
    if u.last_login_at:
        ev.append({"cuando": u.last_login_at, "que": "Ingreso", "detalle": u.last_login_ip or ""})
    usados = []
    for t in tokens:
        quien = f"por {t.created_by}" if t.created_by else "autoservicio"
        ev.append({"cuando": t.created_at, "que": _QUE_CREADO.get(t.purpose, "Link generado"),
                   "detalle": f"{quien} · canal {t.channel}"})
        if t.used_at:
            usados.append(t.used_at)
            ev.append({"cuando": t.used_at, "que": _QUE_USADO.get(t.purpose, "Link usado"), "detalle": ""})
    if u.password_changed_at and not any(abs((u.password_changed_at - x).total_seconds()) <= 2 for x in usados):
        ev.append({"cuando": u.password_changed_at, "que": "Contraseña cambiada",
                   "detalle": "por un administrador"})
    if u.created_at:
        ev.append({"cuando": u.created_at, "que": "Alta",
                   "detalle": f"por {u.created_by}" if u.created_by else ""})
    ev.sort(key=lambda e: e["cuando"], reverse=True)
    for e in ev:
        e["texto"] = fmt_momento(e["cuando"], hoy)
    return ev
```

(`resumen` no cambia; la invalidación de una invitación al emitir un reset NO es un evento nuevo — el token invalidado no tiene `used_at`… sí lo tiene: `issue_reset_token` pone `used_at = now` a los vivos. Para no mostrarlos como "aceptada", `actividad_reciente` sólo cuenta `used_at` como uso real si `t.used_at` está a MÁS de 2 s de la creación del token siguiente… demasiado fino: en su lugar, `issue_reset_token` marca los invalidados con `channel` intacto y `used_at`, y `actividad_reciente` trata como "usado" sólo los tokens cuyo `used_at` coincide (≤ 2 s) con `u.password_changed_at`. Implementarlo así: reemplazar `if t.used_at:` por `if t.used_at and u.password_changed_at and abs((u.password_changed_at - t.used_at).total_seconds()) <= 2:`.)

- [ ] **Step 4: Router `users_abm.py`.** Imports: `from apps.web import reset_service`. Reemplazar `_ctx_ficha` y `_users_page`:

```python
def _ctx_ficha(db, u: UserORM) -> dict:
    inv = reset_service.invitaciones_vivas(db).get(u.id)
    return {"u": u, "v": vista_usuario(u, invitacion=inv),
            "actividad": actividad_reciente(u, tokens=reset_service.tokens_de(db, u)), "TABS": TABS}


def _users_page(request, db, *, status_code: int = 200, selected_id: Optional[int] = None, **ctx):
    """ÚNICA forma de responder la página: arma todo el contexto (filas, resumen, ficha
    seleccionada). `selected_id` mantiene la ficha abierta tras un POST."""
    users = db.query(UserORM).order_by(UserORM.username).all()
    invitaciones = reset_service.invitaciones_vivas(db)
    selected = db.get(UserORM, selected_id) if selected_id is not None else None
    context = {"users": users, "filas": [vista_usuario(u, invitacion=invitaciones.get(u.id)) for u in users],
               "resumen": resumen(users), "TABS": TABS, "selected": selected, **ctx}
    if selected is not None:
        context.update(_ctx_ficha(db, selected))
    return _TEMPLATES.TemplateResponse(request, "pages/users.html", context,
                                       status_code=status_code)
```

y en `ficha()` usar `_ctx_ficha(db, user)`. En `reset_password`, reemplazar el bloque `if channel != "manual": …` por:

```python
    if channel == "link":
        token = reset_service.issue_reset_token(db, user, purpose="reset", channel="link",
                                                by=getattr(admin, "username", None))
        _audit.info("users action=reset_link channel=link purpose=reset by=%s target=%s",
                    _limpio(getattr(admin, "username", "?")), _limpio(user.username),
                    extra={"console": True})
        return _users_page(request, db, selected_id=user_id,
                           link_reset=reset_service.reset_link(request, token), link_para=user.username,
                           link_vence="60 minutos",
                           success=f"Link de reseteo generado para {user.username}. Vence en 60 minutos y sirve una sola vez; los links anteriores quedaron invalidados.")
    if channel != "manual":
        return _users_page(request, db, status_code=400, selected_id=user_id,
                           error="Ese canal de reseteo no está disponible todavía.")
```

- [ ] **Step 5: Templates.** `user_ficha.html`, en `.um-row` de Seguridad, ANTES del botón "Definir contraseña a mano":

```html
        <form method="POST" action="/users/{{ u.id }}/reset"><input type="hidden" name="channel" value="link"><button type="submit" class="btn">Generar link para copiar</button></form>
```

y en la cabecera de la ficha reemplazar el badge de estado por:

```html
        {% if v.estado == "deshabilitado" %}<span class="badge off">Deshabilitado</span>
        {% elif v.estado == "invitado" %}<span class="badge warn">{{ v.invitacion_txt }}</span>
        {% else %}<span class="badge ok">Activo</span>{% endif %}
```

`users.html`: la misma lógica de badge en la columna Estado de la tabla (reemplaza el `{% if u.is_active %}…{% endif %}`), y debajo de los `msg` agregar la caja del link:

```html
  {% if link_reset %}
  <div class="panel" style="margin-bottom: 12px; padding: 12px 14px;">
    <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
      <b>Link para {{ link_para }}</b><span class="mut">vence en {{ link_vence }} · un solo uso · pasáselo por el canal que quieras</span>
    </div>
    <div style="display: flex; gap: 8px; margin-top: 8px;">
      <input id="link-reset" type="text" readonly value="{{ link_reset }}" style="flex: 1; font-family: var(--font-mono); font-size: 12px;" onclick="this.select()">
      <button type="button" class="btn sec" onclick="umCopiarLink()">Copiar</button>
    </div>
    <div class="faint" style="margin-top: 6px;">No se puede volver a ver: si se pierde, generá otro (el anterior deja de servir).</div>
  </div>
  {% endif %}
```

y en el `<script>` la función:

```javascript
function umCopiarLink() {
  const i = document.getElementById("link-reset");
  const b = event.currentTarget;
  window.mrCopy(i.value, (ok) => { b.textContent = ok ? "¡Copiado!" : "Copiá con Ctrl+C"; i.focus(); i.select();
    setTimeout(() => { b.textContent = "Copiar"; }, 2500); });
}
```

- [ ] **Step 6: GREEN** — `-k "fmt_restante or canal_link"` + todo `tests/test_users_manager.py tests/test_aud_D1_seguridad_web.py tests/test_sec_audit_y_passwords.py`. Los tests previos de `vista_usuario`/`actividad_reciente` siguen verdes (los parámetros nuevos tienen default).
- [ ] **Step 7: Commit** — `git commit -m "Manager v2: canal link (token de un solo uso mostrado una vez) y actividad con tokens"`.

---

### Task 6: Alta por invitación

**Files:**
- Modify: `apps/web/routers/users_abm.py` (`add_user`: `access=invite|password`)
- Modify: `apps/web/templates/pages/users.html` (radio "Acceso inicial")
- Test: `tests/test_users_manager.py`

**Produces:** `POST /users/add` con `access=invite` (default `password`) crea el usuario con `hashed_password = SIN_PASSWORD_HASH`, `password_changed_at = None`, emite token `invite` (72 h, canal `link`) y responde con la caja del link (`link_vence="72 horas"`) y la ficha abierta; audita `users action=invite_created by=… target=…`. La invitación NO exige email (deviación explícita de la spec §5.1: el link se copia; el mail llega en la Fase 3 si hay email y SMTP). Con `access=password` el comportamiento actual no cambia. El invitado no puede loguearse hasta aceptar (`verify_password` con `"!"` → False).

- [ ] **Step 1: Tests**

```python
# ── alta por invitación ─────────────────────────────────────────────────────
@pytest.mark.noauth
def test_alta_por_invitacion_crea_sin_clave_y_entrega_el_link(usuarios):
    import re
    from core.security import SIN_PASSWORD_HASH
    with TestClient(app) as admin_c, TestClient(app) as anon:
        _login_admin(admin_c)
        r = admin_c.post("/users/add", data={"username": "jperez", "access": "invite", "full_name": "Juan Pérez",
                                             "tabs": ["bonos"]})
        assert r.status_code == 200 and "Invitación · vence en" in r.text and "72 horas" in r.text
        link = re.search(r'value="(http://testserver/reset/[A-Za-z0-9_\-]+)"', r.text).group(1)
        with SessionLocal() as s:
            j = s.query(UserORM).filter(UserORM.username == "jperez").first()
            assert j.hashed_password == SIN_PASSWORD_HASH and j.password_changed_at is None and j.is_active is True
        assert _login(anon, "jperez", "cualquier-cosa-1").status_code == 200, "el invitado no entra sin aceptar"
        r2 = anon.post(link.replace("http://testserver", ""),
                       data={"password": "clave-de-juan-1", "password2": "clave-de-juan-1"}, follow_redirects=False)
        assert r2.status_code == 303
        assert _login(anon, "jperez", "clave-de-juan-1").status_code in (302, 303)
        ficha = admin_c.get("/users").text
        assert "Invitación · vence en" not in ficha and "Invitación aceptada" in admin_c.get(
            f"/users/{j.id}/ficha").text


@pytest.mark.noauth
def test_alta_con_password_sigue_igual_y_la_invitacion_no_exige_password(usuarios):
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post("/users/add", data={"username": "conclave", "access": "password", "password": "clave-larga-1"}).status_code == 200
        assert c.post("/users/add", data={"username": "sinclave", "access": "password"}).status_code == 400
        assert c.post("/users/add", data={"username": "invitado2", "access": "invite"}).status_code == 200
        assert c.post("/users/add", data={"username": "raro", "access": "otro"}).status_code == 400
```

- [ ] **Step 2: RED**.

- [ ] **Step 3: `add_user`.** Firma: `password: str = Form("")`, `access: str = Form("password")`. Import `from core.security import SIN_PASSWORD_HASH, get_password_hash, password_invalida`. Cuerpo, reemplazando la validación y la construcción:

```python
    mail = normalizar_email(email)
    if access not in ("invite", "password"):
        return _users_page(request, db, status_code=400, abrir_alta=True, error="Acceso inicial inválido.")
    invalido = _username_invalido(username) or email_invalido(mail) or (
        password_invalida(password) if access == "password" else None)
    if invalido:
        return _users_page(request, db, status_code=400, abrir_alta=True, error=invalido)
    if db.query(UserORM).filter(UserORM.username == username).first():
        return _users_page(request, db, status_code=400, abrir_alta=True,
                           error=f"El usuario {username} ya existe.")
    if mail and db.query(UserORM).filter(UserORM.email == mail).first():
        return _users_page(request, db, status_code=400, abrir_alta=True,
                           error=f"Ya hay un usuario con el email {mail}.")

    ahora = datetime.now()
    invitar = access == "invite"
    new_user = UserORM(
        username=username,
        # Invitado: SIN contraseña hasta que acepte el link (centinela, no un hash).
        hashed_password=SIN_PASSWORD_HASH if invitar else get_password_hash(password),
        is_admin=is_admin,
        allowed_tabs=["*"] if is_admin else _tabs_validas(tabs),
        full_name=_texto(full_name, _NOMBRE_MAX),
        email=mail,
        notes=_texto(notes, _NOTAS_MAX),
        is_active=True,
        created_at=ahora,
        created_by=getattr(admin, "username", None),
        password_changed_at=None if invitar else ahora,
    )
    db.add(new_user)
    db.commit()
    _audit.info("users action=add by=%s target=%s is_admin=%s tabs=%s email=%s acceso=%s",
                _limpio(getattr(admin, "username", "?")), _limpio(username),
                bool(is_admin), _limpio(",".join(new_user.allowed_tabs or [])),
                _limpio(mail or "-"), access, extra={"console": True})
    if not invitar:
        return _users_page(request, db, selected_id=new_user.id, success=f"Usuario {username} creado.")
    token = reset_service.issue_reset_token(db, new_user, purpose="invite", channel="link",
                                            by=getattr(admin, "username", None))
    _audit.info("users action=invite_created by=%s target=%s",
                _limpio(getattr(admin, "username", "?")), _limpio(username), extra={"console": True})
    return _users_page(request, db, selected_id=new_user.id,
                       link_reset=reset_service.reset_link(request, token), link_para=username,
                       link_vence="72 horas",
                       success=f"Usuario {username} creado por invitación. Pasale el link: elige su contraseña al abrirlo.")
```

- [ ] **Step 4: Form del alta (`users.html`).** Reemplazar el `<label class="fld"><span>Contraseña inicial *</span>…</label>` por una sección "Acceso inicial" ANTES de los permisos:

```html
        <div>
          <p class="um-sect-t">Acceso inicial</p>
          <div style="display: flex; gap: 10px; flex-wrap: wrap;">
            <label class="perm" style="flex: 1; min-width: 240px; align-items: flex-start; border: 1px solid var(--panel-border); border-radius: 6px; padding: 10px 12px;">
              <input type="radio" name="access" value="invite" checked onchange="umAcceso(this)" style="margin-top: 3px;">
              <span><b>Invitación con link</b><br><span class="faint">Recibe un link (vence en 72 h) y elige su propia contraseña. Nadie más la conoce.</span></span>
            </label>
            <label class="perm" style="flex: 1; min-width: 240px; align-items: flex-start; border: 1px solid var(--panel-border); border-radius: 6px; padding: 10px 12px;">
              <input type="radio" name="access" value="password" onchange="umAcceso(this)" style="margin-top: 3px;">
              <span><b>Contraseña inicial a mano</b><br><span class="faint">La generás acá y se la pasás vos.</span></span>
            </label>
          </div>
          <div id="alta-pwd" hidden style="margin-top: 10px;">
            <label class="fld"><span>Contraseña inicial *</span>
              <span style="display: flex; gap: 6px;"><input type="password" name="password" id="new-user-pwd" autocomplete="new-password" minlength="10">
                <button type="button" class="btn sec" onclick="genPwdForInput('new-user-pwd')" title="Generar contraseña segura">Generar</button></span></label>
          </div>
        </div>
```

y en el `<script>`:

```javascript
function umAcceso(radio) {
  const caja = document.getElementById("alta-pwd");
  const input = document.getElementById("new-user-pwd");
  const manual = radio.value === "password" && radio.checked;
  caja.hidden = !manual;
  input.required = manual;
  if (!manual) input.value = "";
}
```

(El input de contraseña deja de ser `required` salvo en modo manual; el servidor valida igual.)

- [ ] **Step 5: GREEN** — `-k "invitacion"` + `tests/test_users_manager.py tests/test_sec_audit_y_passwords.py tests/test_aud_D1_seguridad_web.py` (los tests viejos del alta mandan `password` sin `access` → default `password` → sin cambios).
- [ ] **Step 6: Commit** — `git commit -m "Manager v2: alta por invitación (usuario sin clave + link de 72 h), contraseña a mano como alternativa"`.

---

### Task 7: Docs, gate, security review y cierre

**Files:** `.claude/rules/auth.md`, `docs/auth.md`, `CLAUDE.md` (una línea), spec §5.1 (deviación: la invitación no exige email).

- [ ] **Step 1: `.claude/rules/auth.md`** — en "Rutas públicas" agregar `/reset/{token}`; en `## Usuarios` sumar: canal `link` (`/users/{id}/reset channel=link`), alta `access=invite` (`hashed_password="!"`, 72 h), tokens en `apps/web/reset_service.py` (contrato: hash, un uso, TTL, invalida anteriores, inactivo inválido, sube `token_version`), login por usuario o email, `verify_password` robusto.
- [ ] **Step 2: `docs/auth.md`** — sección "Manager de usuarios": párrafo "Reseteo por link e invitación" con lo mismo en prosa; sección "Rutas públicas" mencionando `/reset/{token}` y el rate-limit.
- [ ] **Step 3: `CLAUDE.md`** — línea **Web** "Rutas públicas = …": `(/login, /logout, /api/health, /reset/{token}, /static)`.
- [ ] **Step 4: Spec §5.1** — fila `POST /users/add`: cambiar "`invite` exige email" por "`invite` no exige email (el link se copia; con email + SMTP además se manda, Fase 3)".
- [ ] **Step 5: Gate completo** — ruff + `pytest tests/ -q -p no:cacheprovider` con `MONITOR_TEST_DB_DIR`. Verde.
- [ ] **Step 6: Commit** — `git commit -m "Docs: reseteo por link, invitación y ruta pública /reset/{token}"`.
- [ ] **Step 7 (controller):** `/security-review`, smoke visual en :8001 (modal editable, generar link, abrir el link en otra sesión, invitación), review final, merge a `main`.

## Self-review

- **Cobertura F2 (spec §9)**: tabla + contrato §3 (T3), `/reset/{token}` §5.2 + banner §5.3 (T4), canal link §5.1 (T5), alta por invitación §5.1 (T6), actividad §2.3 con tokens (T5), `_PUBLIC_PATHS` (T4), rate-limit de `/reset` §5.4 (T4), deltas login por email §5.3 (T1) y a mano editable §6 (T2), docs (T7).
- **Sin placeholders**: código y tests completos en cada task; la nota de T5 sobre "usado real vs invalidado" está resuelta en el propio texto (condición `used_at` ≈ `password_changed_at`).
- **Consistencia**: `reset_service.issue_reset_token(db, user, *, purpose, channel, by)` igual en T3/T5/T6; `reset_link(request, token)`; `invitaciones_vivas(db)`; `tokens_de(db, user)`; `_ctx_ficha(db, u)` en T5 y en `ficha()`; contexto `link_reset/link_para/link_vence` en T5, T6 y `users.html`; `SIN_PASSWORD_HASH` en T3/T5/T6; `_rate_limited` sólo en `auth.py`.
