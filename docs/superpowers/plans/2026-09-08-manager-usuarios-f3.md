# Manager de usuarios v2 — Fase 3 · Plan de implementación (mail, canal `mail`, `/forgot`, nginx, docs)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que el link de reseteo/invitación llegue por mail (Gmail SMTP), que el admin tenga el botón "Enviar link por mail" en la ficha, y que el login tenga "¿Olvidaste tu contraseña?" con una página de autoservicio que no revela si la cuenta existe.

**Architecture:** `core/infrastructure/mailer.py` manda con `smtplib` + STARTTLS (stdlib, sin dependencia nueva), configurado por `settings.smtp_*`; `settings.mail_enabled` es la perilla (host vacío = apagado). Los textos del mail viven en `apps/web/mail_templates.py` (puros: devuelven asunto, texto y HTML sin recursos externos). El canal `mail` del Manager y la invitación por mail reutilizan `reset_service.issue_reset_token` y mandan **en el request** (sincrónico dentro del handler sync, timeout 15 s) para poder decirle al admin si falló y darle el link igual. `/forgot` (público) emite el token y manda **en background** (`BackgroundTasks`) para que el tiempo de respuesta no dependa de si el usuario existe; responde siempre la misma página. nginx gana `limit_req` en `/forgot` y `/reset/`.

**Tech Stack:** FastAPI 0.141 (`BackgroundTasks`) · `smtplib`/`ssl`/`email.message` stdlib · pydantic-settings · Jinja2 · pytest con `monkeypatch` sobre `smtplib.SMTP` y sobre el mailer.

**Spec:** `docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md` (§4, §5.1 canal `mail` e invitación por mail, §5.2 `/forgot`, §5.3 link en login, §5.4 rate-limit, §7, §8, fila F3 de §9, §10).

## Global Constraints

- Intérprete siempre `py -3.12`. Pytest de subagentes con `MONITOR_TEST_DB_DIR=C:\Users\david\AppData\Local\Temp\monitor_pytest_f3`. `py -3.12 -m pytest <files> -q -p no:cacheprovider`.
- **Sin dependencia nueva**: `smtplib`, `ssl`, `email.message.EmailMessage`, `asyncio`. TLS: `smtplib.SMTP(host, port, timeout=15)` → `starttls(context=ssl.create_default_context())` → `login` → `send_message`. El timeout NUNCA es `None`.
- Secretos: `MONITOR_SMTP_PASSWORD` sólo por env/`.env` del servidor; nunca en el repo, nunca en logs. El token nunca se loguea ni se manda a ningún lado salvo al destinatario.
- Anti-enumeración en `/forgot`: la respuesta (status, cuerpo) es idéntica exista o no el usuario, tenga o no email, esté o no activo; el envío va en background. Rate-limit: 3 por IP / 15 min **y** 3 por dato tipeado / 60 min → 429 con la misma página y un aviso genérico.
- `mail_enabled = bool(settings.smtp_host)`. Con mail apagado: el canal `mail` responde 400 con motivo, la ficha muestra el botón deshabilitado con el motivo, `/forgot` GET muestra "pedile el link a tu administrador" sin formulario y el POST responde la misma página neutra sin hacer nada.
- Rutas públicas: se suma **sólo** `/forgot` (`_PUBLIC_PATHS` a sabiendas). `/reset/{token}` ya es pública.
- Auditoría `monitor.audit`: `reset_link channel=mail purpose=… by=… target=… sent=ok|fail`, `invite_sent target=… sent=ok|fail`, `forgot=requested target=…` (sólo si existía y se emitió) / `forgot=noop` (sin decir por qué), `forgot=ratelimited ip=…`. Nunca el email completo del destinatario en el log? → SÍ se puede loguear el username; el email NO (PII): loguear `target=<username>` solamente.
- HTML del mail: inline, sin imágenes ni hosts externos; texto plano siempre presente.
- Commits por tarea en la rama `feat/manager-usuarios-f3` (worktree `~/.config/superpowers/worktrees/monitores/feat-manager-usuarios-f3`), castellano, `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Nunca push/deploy desde una tarea.
- `/security-review` antes de pushear (rutas públicas y auth).

---

### Task 0: Rama, worktree, baseline

- [ ] `git worktree add "$HOME/.config/superpowers/worktrees/monitores/feat-manager-usuarios-f3" -b feat/manager-usuarios-f3 main`; `EnterWorktree(path=...)`; copiar este plan al worktree; baseline `ruff` + `pytest tests/` (verde: 3171 en `main` fa88e54); `git commit` del plan: "Manager v2: plan de la Fase 3 (mail, /forgot)".

---

### Task 1: Settings de SMTP + mailer + plantillas de mail

**Files:**
- Modify: `config/settings.py` (campos `smtp_*` + property `mail_enabled`, debajo de `public_url`)
- Create: `core/infrastructure/mailer.py`
- Create: `apps/web/mail_templates.py`
- Test: `tests/test_mailer.py` (nuevo)

**Interfaces (Produces):**
- `settings.smtp_host: str = ""`, `smtp_port: int = 587`, `smtp_user: str = ""`, `smtp_password: str = ""`, `smtp_from: str = ""`; `settings.mail_enabled -> bool` (`bool(smtp_host)`); `settings.smtp_sender -> str` (`smtp_from or smtp_user`).
- `core.infrastructure.mailer.send_mail(to: str, subject: str, text: str, html: str | None = None) -> None` (sync; lanza `MailNotConfigured` si `not settings.mail_enabled`; propaga `smtplib.SMTPException`/`OSError`); `asend_mail(...)` = `asyncio.to_thread(send_mail, ...)`; `class MailNotConfigured(RuntimeError)`.
- `apps.web.mail_templates.mail_reset(nombre: str, username: str, link: str, minutos: int, por: str | None) -> tuple[str, str, str]` (asunto, texto, html) y `mail_invitacion(nombre, username, link, horas: int, por) -> tuple[str, str, str]`. Sin hosts externos en el HTML; el link aparece en texto y en HTML (botón + URL en claro).

- [ ] **Step 1: Tests** (`tests/test_mailer.py`):

```python
"""Mailer SMTP (stdlib) + plantillas de mail del Manager v2 (spec §4). Sin red: smtplib.SMTP se mockea."""
import re
import ssl

import pytest

from config.settings import settings


@pytest.fixture
def smtp_config(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.gmail.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_user", "monitor@ejemplo.com")
    monkeypatch.setattr(settings, "smtp_password", "app-password")
    monkeypatch.setattr(settings, "smtp_from", "")
    yield


class _FakeSMTP:
    instancias = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.llamadas = []
        _FakeSMTP.instancias.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.llamadas.append(("quit",))

    def starttls(self, context=None):
        self.llamadas.append(("starttls", context))

    def login(self, user, pw):
        self.llamadas.append(("login", user, pw))

    def send_message(self, msg):
        self.llamadas.append(("send", msg))


def test_mail_enabled_depende_del_host(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    assert settings.mail_enabled is False
    monkeypatch.setattr(settings, "smtp_host", "smtp.gmail.com")
    assert settings.mail_enabled is True


def test_send_mail_usa_starttls_con_contexto_login_y_timeout(smtp_config, monkeypatch):
    import smtplib
    from core.infrastructure import mailer
    _FakeSMTP.instancias.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    mailer.send_mail("m.caceres@ejemplo.com", "Asunto", "texto plano", "<p>html</p>")
    s = _FakeSMTP.instancias[-1]
    assert (s.host, s.port) == ("smtp.gmail.com", 587)
    assert isinstance(s.timeout, (int, float)) and s.timeout is not None
    nombres = [c[0] for c in s.llamadas]
    assert nombres == ["starttls", "login", "send", "quit"]
    assert isinstance(s.llamadas[0][1], ssl.SSLContext)
    assert s.llamadas[1][1:] == ("monitor@ejemplo.com", "app-password")
    msg = s.llamadas[2][1]
    assert msg["To"] == "m.caceres@ejemplo.com" and msg["From"] == "monitor@ejemplo.com" and msg["Subject"] == "Asunto"
    assert msg.get_body(preferencelist=("plain",)).get_content().strip() == "texto plano"
    assert "<p>html</p>" in msg.get_body(preferencelist=("html",)).get_content()


def test_send_mail_sin_configurar_lanza_y_no_toca_la_red(monkeypatch):
    import smtplib
    from core.infrastructure import mailer
    monkeypatch.setattr(settings, "smtp_host", "")
    def _boom(*a, **k):
        raise AssertionError("no debe abrir SMTP")
    monkeypatch.setattr(smtplib, "SMTP", _boom)
    with pytest.raises(mailer.MailNotConfigured):
        mailer.send_mail("a@b.co", "x", "y")


def test_plantillas_traen_el_link_en_texto_y_html_sin_hosts_externos():
    from apps.web.mail_templates import mail_invitacion, mail_reset
    link = "http://129.80.148.166/reset/abcDEF123"
    for asunto, texto, html in (mail_reset("Mariana", "mcaceres", link, 60, "admin"),
                                mail_invitacion("Juan", "jperez", link, 72, "admin")):
        assert asunto and "Monitor" in asunto
        assert link in texto and link in html
        assert "mcaceres" in texto or "jperez" in texto
        assert re.search(r'https?://(?!129\.80\.148\.166)[^"\'\s>]+', html) is None, "el HTML no puede cargar nada externo"
        assert "<img" not in html and "<script" not in html
    a, t, h = mail_reset("Mariana", "mcaceres", link, 60, None)
    assert "60 minutos" in t and "una sola vez" in t
    a2, t2, _ = mail_invitacion("Juan", "jperez", link, 72, "admin")
    assert "72 horas" in t2 and "admin" in t2
```

- [ ] **Step 2: RED** — `py -3.12 -m pytest tests/test_mailer.py -q -p no:cacheprovider` → FAIL (no existe `mail_enabled`, ni `mailer`, ni `mail_templates`).

- [ ] **Step 3: `config/settings.py`** — debajo de `public_url`:

```python
    # --- Correo saliente (Manager v2, spec §4): SMTP con STARTTLS, stdlib. Host vacío =
    # correo APAGADO (el Manager esconde el canal mail y /forgot no manda nada). Gmail:
    # smtp.gmail.com:587 con contraseña de aplicación (exige 2FA en la cuenta). La
    # contraseña SOLO por env/.env del servidor (MONITOR_SMTP_PASSWORD), nunca en el repo.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""          # remitente visible; vacío = smtp_user

    @property
    def mail_enabled(self) -> bool:
        return bool(self.smtp_host)

    @property
    def smtp_sender(self) -> str:
        return self.smtp_from or self.smtp_user
```

(pydantic v2 permite `@property` en `BaseSettings`; si el modelo tiene `model_config` con `extra="ignore"` no hay conflicto.)

- [ ] **Step 4: `core/infrastructure/mailer.py`**:

```python
"""Correo saliente por SMTP con STARTTLS (stdlib). Un solo camino: `send_mail` (sync, para
llamarlo desde handlers sync o vía `asend_mail` en los async). Sin reintentos: el llamador
decide (el admin ve el error y recibe el link igual; /forgot loguea WARNING y no cambia su
respuesta). El timeout NUNCA es None (invariante httpx del repo, misma idea). TLS verificado
siempre con el contexto por default del sistema (spec §4)."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

from config.settings import settings

SMTP_TIMEOUT_S = 15


class MailNotConfigured(RuntimeError):
    """`settings.smtp_host` vacío: el correo está apagado a propósito."""


def send_mail(to: str, subject: str, text: str, html: Optional[str] = None) -> None:
    if not settings.mail_enabled:
        raise MailNotConfigured("MONITOR_SMTP_HOST vacío: el correo está desactivado")
    msg = EmailMessage()
    msg["From"] = settings.smtp_sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_S) as s:
        s.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)


async def asend_mail(to: str, subject: str, text: str, html: Optional[str] = None) -> None:
    await asyncio.to_thread(send_mail, to, subject, text, html)
```

- [ ] **Step 5: `apps/web/mail_templates.py`**:

```python
"""Textos de los mails del Manager (reseteo e invitación). Puros: (asunto, texto, html).
El HTML es inline y no carga NADA externo (ni imágenes ni CSS): lo que ve el cliente de
correo es lo que está acá. El link aparece como botón y como URL en claro."""

from __future__ import annotations

from html import escape
from typing import Optional

_PIE = "Monitor Renta Fija AR · mail automático, no respondas a esta dirección."


def _html(saludo: str, cuerpo: str, boton: str, link: str, nota: str) -> str:
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;line-height:1.55;'
        'color:#1f2328;max-width:560px;margin:0 auto;padding:24px">'
        f'<p style="margin:0 0 12px">{escape(saludo)}</p>'
        f'<p style="margin:0 0 18px">{cuerpo}</p>'
        f'<p style="text-align:center;margin:0 0 18px"><a href="{escape(link)}" '
        'style="display:inline-block;background:#2962ff;color:#fff;font-weight:600;padding:11px 22px;'
        f'border-radius:6px;text-decoration:none">{escape(boton)}</a></p>'
        f'<p style="margin:0 0 6px;color:#57606a;font-size:13px">{nota}</p>'
        '<p style="margin:0 0 18px;color:#57606a;font-size:13px">Si no lo pediste, ignorá este mail: tu '
        'contraseña actual sigue igual.</p>'
        '<p style="margin:0;color:#57606a;font-size:12px">Si el botón no funciona, copiá esta dirección en '
        f'el navegador:<br><span style="font-family:Consolas,monospace;word-break:break-all">{escape(link)}</span></p>'
        f'<p style="margin:18px 0 0;color:#8b949e;font-size:11px">{escape(_PIE)}</p></div>'
    )


def mail_reset(nombre: str, username: str, link: str, minutos: int, por: Optional[str]) -> tuple[str, str, str]:
    quien = f"{por}, administrador del Monitor," if por else "Vos (o alguien con tu usuario)"
    asunto = "Tu link para elegir una contraseña nueva — Monitor Renta Fija AR"
    texto = (
        f"Hola {nombre},\n\n"
        f"{quien} pidió restablecer la contraseña de tu usuario {username} en el Monitor.\n"
        f"Entrá al link y elegí una nueva:\n\n{link}\n\n"
        f"El link vence en {minutos} minutos y sirve una sola vez.\n"
        "Si no lo pediste, ignorá este mail: tu contraseña actual sigue igual.\n\n"
        f"{_PIE}\n"
    )
    html = _html(f"Hola {nombre},",
                 f"{escape(quien)} pidió restablecer la contraseña de tu usuario <b>{escape(username)}</b> en el "
                 "Monitor. Entrá al link y elegí una nueva:",
                 "Elegir contraseña nueva", link,
                 f"El link vence en <b>{minutos} minutos</b> y sirve una sola vez.")
    return asunto, texto, html


def mail_invitacion(nombre: str, username: str, link: str, horas: int, por: Optional[str]) -> tuple[str, str, str]:
    quien = f"{por}, administrador del Monitor," if por else "Un administrador del Monitor"
    asunto = "Te invitaron al Monitor Renta Fija AR"
    texto = (
        f"Hola {nombre},\n\n"
        f"{quien} te creó el usuario {username} en el Monitor Renta Fija AR.\n"
        f"Entrá al link y elegí tu contraseña:\n\n{link}\n\n"
        f"El link vence en {horas} horas y sirve una sola vez.\n"
        "Si no lo esperabas, ignorá este mail.\n\n"
        f"{_PIE}\n"
    )
    html = _html(f"Hola {nombre},",
                 f"{escape(quien)} te creó el usuario <b>{escape(username)}</b> en el Monitor Renta Fija AR. "
                 "Entrá al link y elegí tu contraseña:",
                 "Elegir mi contraseña", link,
                 f"El link vence en <b>{horas} horas</b> y sirve una sola vez.")
    return asunto, texto, html
```

- [ ] **Step 6: GREEN** — `tests/test_mailer.py` + `tests/test_aud_A_infra_http_timeouts.py` (por si mira wrappers) + ruff.
- [ ] **Step 7: Commit** — "Correo saliente: settings SMTP, mailer stdlib con STARTTLS y plantillas de reseteo/invitación".

---

### Task 2: Canal `mail` en el Manager e invitación por mail

**Files:**
- Modify: `apps/web/routers/users_abm.py` (`reset_password` canal `mail`; `add_user` manda la invitación si hay email y mail)
- Modify: `apps/web/templates/fragments/user_ficha.html` (botón "Enviar link por mail")
- Modify: `apps/web/templates/pages/users.html` (texto de la caja del link cuando además se mandó)
- Test: `tests/test_users_manager.py`

**Interfaces:**
- `POST /users/{id}/reset channel=mail`: requiere `settings.mail_enabled` y `user.email` (si no → 400 con motivo: "El correo no está configurado en el servidor." / "El usuario no tiene email cargado."). Emite el token (`purpose=reset, channel=mail`), arma el link, manda con `mailer.send_mail(user.email, *mail_reset(nombre, username, link, 60, admin.username))` **dentro del handler** (sync). Éxito → `_users_page(selected_id, success="Link enviado a <email>. Vence en 60 minutos.", link_reset=None)`; fallo (`Exception`) → `_users_page(status_code=200, selected_id, error="No se pudo mandar el mail (<clase del error>). Pasale este link a mano:", link_reset=<url>, link_para, link_vence="60 minutos")`. Auditoría `users action=reset_link channel=mail purpose=reset by=… target=… sent=ok|fail`.
- `POST /users/add access=invite`: si `mail_enabled` y `mail` → además manda `mail_invitacion(nombre, username, link, 72, admin.username)`; éxito → success "Usuario X creado por invitación; el link se mandó a <email> (vence en 72 h)" y la caja del link igual (por si quiere pasarlo también); fallo → error "Usuario creado pero el mail falló (<clase>). Pasale el link:" + caja. Auditoría `invite_sent target=… sent=ok|fail`.
- Ficha: botón `<form method="POST" action="/users/{{ u.id }}/reset"><input type="hidden" name="channel" value="mail"><button class="btn" {% if not mail_enabled or not u.email %}disabled title="…motivo…"{% endif %}>Enviar link por mail</button></form>`; `mail_enabled` llega al contexto desde `_ctx_ficha` (`"mail_enabled": settings.mail_enabled`). El botón "Generar link para copiar" pasa a `btn sec`.

- [ ] **Step 1: Tests** (stub del mailer con `monkeypatch.setattr("apps.web.routers.users_abm.send_mail", fake)`; el router importa `from core.infrastructure.mailer import send_mail`):

```python
# ── canal mail ──────────────────────────────────────────────────────────────
@pytest.fixture
def mail_on(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_user", "monitor@test")
    enviados = []
    def fake_send(to, subject, text, html=None):
        enviados.append({"to": to, "subject": subject, "text": text, "html": html})
    monkeypatch.setattr("apps.web.routers.users_abm.send_mail", fake_send)
    return enviados


@pytest.mark.noauth
def test_canal_mail_manda_el_link_y_no_lo_muestra(usuarios, mail_on):
    import re
    bob = _bob_id()
    with TestClient(app) as admin_c, TestClient(app) as anon:
        _login_admin(admin_c)
        r = admin_c.post(f"/users/{bob}/reset", data={"channel": "mail"})
        assert r.status_code == 200 and "Link enviado a bob@ejemplo.com" in r.text
        assert 'id="link-reset"' not in r.text, "si el mail salió, el link no se muestra"
        assert len(mail_on) == 1 and mail_on[0]["to"] == "bob@ejemplo.com"
        link = re.search(r"http://testserver/reset/[A-Za-z0-9_\-]+", mail_on[0]["text"]).group(0)
        assert "Bob" in mail_on[0]["text"] and "admin" in mail_on[0]["text"]
        assert anon.get(link.replace("http://testserver", "")).status_code == 200
        assert 'name="password2"' in anon.get(link.replace("http://testserver", "")).text


@pytest.mark.noauth
def test_canal_mail_si_falla_el_envio_muestra_el_link(usuarios, mail_on, monkeypatch):
    def boom(*a, **k):
        raise OSError("SMTP caído")
    monkeypatch.setattr("apps.web.routers.users_abm.send_mail", boom)
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post(f"/users/{bob}/reset", data={"channel": "mail"})
    assert r.status_code == 200 and "No se pudo mandar el mail" in r.text and 'id="link-reset"' in r.text


@pytest.mark.noauth
def test_canal_mail_sin_email_o_sin_smtp_da_400(usuarios, monkeypatch):
    from config.settings import settings
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        monkeypatch.setattr(settings, "smtp_host", "")
        r = c.post(f"/users/{bob}/reset", data={"channel": "mail"})
        assert r.status_code == 400 and "no está configurado" in r.text
        ficha = c.get(f"/users/{bob}/ficha").text
        assert "Enviar link por mail" in ficha and "disabled" in ficha
        monkeypatch.setattr(settings, "smtp_host", "smtp.test")
        _set_bob(email=None)
        r = c.post(f"/users/{bob}/reset", data={"channel": "mail"})
        assert r.status_code == 400 and "no tiene email" in r.text


@pytest.mark.noauth
def test_invitacion_con_email_y_smtp_manda_el_mail(usuarios, mail_on):
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post("/users/add", data={"username": "jperez", "access": "invite", "email": "jperez@ejemplo.com",
                                       "full_name": "Juan Pérez"})
        assert r.status_code == 200 and "se mandó a jperez@ejemplo.com" in r.text and 'id="link-reset"' in r.text
    assert len(mail_on) == 1 and mail_on[0]["to"] == "jperez@ejemplo.com" and "72 horas" in mail_on[0]["text"]
    assert "Te invitaron" in mail_on[0]["subject"]


@pytest.mark.noauth
def test_invitacion_sin_email_no_intenta_mandar(usuarios, mail_on):
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post("/users/add", data={"username": "sinmail", "access": "invite"}).status_code == 200
    assert mail_on == []
```

- [ ] **Step 2: RED**.
- [ ] **Step 3: Router.** Imports: `from config.settings import settings`, `from core.infrastructure.mailer import send_mail`, `from apps.web.mail_templates import mail_invitacion, mail_reset`. `_ctx_ficha`: sumar `"mail_enabled": settings.mail_enabled`. En `reset_password`, antes del `if channel == "link":`:

```python
    if channel == "mail":
        if not settings.mail_enabled:
            return _users_page(request, db, status_code=400, selected_id=user_id,
                               error="El correo no está configurado en el servidor (MONITOR_SMTP_HOST). Usá «Generar link para copiar».")
        if not user.email:
            return _users_page(request, db, status_code=400, selected_id=user_id,
                               error=f"{user.username} no tiene email cargado. Cargalo en Datos o usá el link para copiar.")
        token = reset_service.issue_reset_token(db, user, purpose="reset", channel="mail",
                                                by=getattr(admin, "username", None))
        link = reset_service.reset_link(request, token)
        nombre = (user.full_name or user.username).split()[0]
        try:
            send_mail(user.email, *mail_reset(nombre, user.username, link, 60, getattr(admin, "username", None)))
        except Exception as e:   # noqa: BLE001 — se informa al admin y se le da el link igual
            _audit.info("users action=reset_link channel=mail purpose=reset by=%s target=%s sent=fail err=%s",
                        _limpio(getattr(admin, "username", "?")), _limpio(user.username),
                        _limpio(type(e).__name__), extra={"console": True})
            return _users_page(request, db, selected_id=user_id, link_reset=link, link_para=user.username,
                               link_vence="60 minutos",
                               error=f"No se pudo mandar el mail ({type(e).__name__}). Pasale este link a mano:")
        _audit.info("users action=reset_link channel=mail purpose=reset by=%s target=%s sent=ok",
                    _limpio(getattr(admin, "username", "?")), _limpio(user.username), extra={"console": True})
        return _users_page(request, db, selected_id=user_id,
                           success=f"Link enviado a {user.email}. Vence en 60 minutos y sirve una sola vez.")
```

En `add_user`, rama `invitar`, después de emitir el token y auditar `invite_created`:

```python
    link = reset_service.reset_link(request, token)
    if settings.mail_enabled and mail:
        nombre = (new_user.full_name or new_user.username).split()[0]
        try:
            send_mail(mail, *mail_invitacion(nombre, username, link, 72, getattr(admin, "username", None)))
            _audit.info("users action=invite_sent target=%s sent=ok", _limpio(username), extra={"console": True})
            return _users_page(request, db, selected_id=new_user.id, link_reset=link, link_para=username,
                               link_vence="72 horas",
                               success=f"Usuario {username} creado por invitación; el link se mandó a {mail} (vence en 72 horas). Acá lo tenés también por si querés pasárselo vos.")
        except Exception as e:   # noqa: BLE001
            _audit.info("users action=invite_sent target=%s sent=fail err=%s", _limpio(username),
                        _limpio(type(e).__name__), extra={"console": True})
            return _users_page(request, db, selected_id=new_user.id, link_reset=link, link_para=username,
                               link_vence="72 horas",
                               error=f"Usuario {username} creado, pero el mail falló ({type(e).__name__}). Pasale el link a mano:")
    return _users_page(request, db, selected_id=new_user.id, link_reset=link, link_para=username,
                       link_vence="72 horas",
                       success=f"Usuario {username} creado por invitación. Pasale el link: elige su contraseña al abrirlo.")
```

- [ ] **Step 4: Ficha** (`user_ficha.html`, sección Seguridad, primer botón):

```html
        <form method="POST" action="/users/{{ u.id }}/reset"><input type="hidden" name="channel" value="mail">
          <button type="submit" class="btn" {% if not mail_enabled %}disabled title="El correo no está configurado en el servidor"{% elif not u.email %}disabled title="El usuario no tiene email cargado"{% endif %}>Enviar link por mail</button></form>
        <form method="POST" action="/users/{{ u.id }}/reset"><input type="hidden" name="channel" value="link"><button type="submit" class="btn sec">Generar link para copiar</button></form>
```

(y en `users.html` agregar `.btn[disabled] { opacity: .45; cursor: not-allowed; }`).

- [ ] **Step 5: GREEN** — `tests/test_users_manager.py tests/test_aud_D1_seguridad_web.py tests/test_sec_audit_y_passwords.py tests/test_mailer.py` + ruff. Verificar que el token NO aparece en ninguna línea de auditoría (el test guardián `test_el_token_no_aparece_en_la_auditoria` sigue verde).
- [ ] **Step 6: Commit** — "Manager v2: canal mail (reseteo e invitación por correo, con el link de respaldo si el envío falla)".

---

### Task 3: `/forgot` público + link en el login

**Files:**
- Modify: `apps/web/routers/auth.py` (`GET/POST /forgot`, limiter doble, `BackgroundTasks`)
- Create: `apps/web/templates/pages/forgot.html`, `apps/web/templates/pages/forgot_enviado.html`
- Modify: `apps/web/templates/pages/login.html` (link "¿Olvidaste tu contraseña?")
- Modify: `tests/test_aud_G_tests_route_auth.py:31` (`_PUBLIC_PATHS` += `/forgot`)
- Test: `tests/test_users_manager.py`

**Interfaces:**
- `GET /forgot`: 200 `forgot.html` (formulario "Usuario o email") si `settings.mail_enabled`; si no, 200 la misma plantilla con `sin_mail=True` (texto "El envío de mails no está habilitado. Pedile el link a tu administrador." y sin formulario).
- `POST /forgot` (form `dato`): rate-limit por IP (3/15 min) y por `dato` normalizado (3/60 min) → 429 con `forgot_enviado.html` y `ratelimited=True`; si no, SIEMPRE 200 `forgot_enviado.html` ("Revisá tu correo. Si existe una cuenta con ese dato…"). Si `mail_enabled` y el dato corresponde a un usuario activo con email (por `username == dato` o `email == normalizar_email(dato)`), emite token (`purpose=reset, channel=self, by=None`), arma el link y encola `background_tasks.add_task(_mandar_reset_en_background, email, nombre, username, link)` que llama `send_mail` y loguea `WARNING` si falla. Auditoría: `auth forgot=requested target=<username> ip=…` sólo en ese caso; `auth forgot=noop ip=…` en cualquier otro (sin decir por qué).
- `login.html`: debajo del botón, `<div class="links"><a href="/forgot">¿Olvidaste tu contraseña?</a></div>` (siempre visible).
- `reset_invalido.html`: agregar el link "Pedir un link nuevo" → `/forgot`.

- [ ] **Step 1: Tests**

```python
# ── /forgot ─────────────────────────────────────────────────────────────────
@pytest.mark.noauth
def test_forgot_responde_igual_exista_o_no_y_solo_manda_al_valido(usuarios, mail_on, monkeypatch):
    from apps.web.routers import auth as auth_router
    monkeypatch.setattr("apps.web.routers.auth.send_mail", lambda to, s, t, h=None: mail_on.append({"to": to, "text": t}))
    auth_router._forgot_attempts_ip.clear(); auth_router._forgot_attempts_dato.clear()
    _set_bob(is_active=True)
    with TestClient(app) as c:
        assert "¿Olvidaste tu contraseña?" in c.get("/login").text
        assert 'name="dato"' in c.get("/forgot").text
        r_user = c.post("/forgot", data={"dato": "bob"})
        r_mail = c.post("/forgot", data={"dato": "BOB@ejemplo.com"})
        r_nadie = c.post("/forgot", data={"dato": "nadie@ejemplo.com"})
        _set_bob(is_active=False)
        r_off = c.post("/forgot", data={"dato": "bob"})
        _set_bob(is_active=True, email=None)
        r_sinmail = c.post("/forgot", data={"dato": "bob"})
    assert r_user.status_code == r_mail.status_code == r_nadie.status_code == r_off.status_code == r_sinmail.status_code == 200
    assert r_user.text == r_mail.text == r_nadie.text == r_off.text == r_sinmail.text
    assert "Revisá tu correo" in r_user.text
    assert len(mail_on) == 2 and all(m["to"] == "bob@ejemplo.com" for m in mail_on)
    import re
    link = re.search(r"http://testserver/reset/[A-Za-z0-9_\-]+", mail_on[-1]["text"]).group(0)
    with TestClient(app) as anon:
        assert 'name="password2"' in anon.get(link.replace("http://testserver", "")).text


@pytest.mark.noauth
def test_forgot_con_mail_apagado_no_hace_nada_y_lo_dice(usuarios, monkeypatch):
    from config.settings import settings
    from core.infrastructure.db.models import PasswordResetTokenORM
    monkeypatch.setattr(settings, "smtp_host", "")
    with TestClient(app) as c:
        g = c.get("/forgot")
        assert g.status_code == 200 and "Pedile el link a tu administrador" in g.text and 'name="dato"' not in g.text
        r = c.post("/forgot", data={"dato": "bob"})
        assert r.status_code == 200 and "Revisá tu correo" in r.text
    with SessionLocal() as s:
        assert s.query(PasswordResetTokenORM).count() == 0


@pytest.mark.noauth
def test_forgot_rate_limit_por_ip_y_por_dato(usuarios, mail_on):
    from apps.web.routers import auth as auth_router
    auth_router._forgot_attempts_ip.clear(); auth_router._forgot_attempts_dato.clear()
    with TestClient(app) as c:
        codigos = [c.post("/forgot", data={"dato": f"x{i}@ejemplo.com"}).status_code for i in range(4)]
    assert codigos == [200, 200, 200, 429]
    auth_router._forgot_attempts_ip.clear()
    with TestClient(app) as c:
        codigos = [c.post("/forgot", data={"dato": "bob"}).status_code for i in range(4)]
    assert codigos[3] == 429, "el mismo dato tiene su propio límite (3 por hora)"
    auth_router._forgot_attempts_ip.clear(); auth_router._forgot_attempts_dato.clear()
```

`_PUBLIC_PATHS = {"/login", "/logout", "/api/health", "/reset/{token}", "/forgot"}` (comentario: `# /forgot: autoservicio, responde siempre igual (spec §5.2)`).

- [ ] **Step 2: RED**.
- [ ] **Step 3: Router `auth.py`.** Imports: `from fastapi import BackgroundTasks`, `from core.infrastructure.mailer import send_mail`, `from apps.web.mail_templates import mail_reset`. Buckets: `_forgot_attempts_ip: dict = defaultdict(list)`, `_forgot_attempts_dato: dict = defaultdict(list)`; constantes `_FORGOT_IP = (3, 900)`, `_FORGOT_DATO = (3, 3600)`.

```python
# ── Autoservicio "olvidé mi contraseña" ───────────────────────────────────────
# Pública a sabiendas. Responde SIEMPRE la misma página, exista o no el usuario, tenga o
# no email, esté o no activo; el mail se manda en background para que el tiempo de
# respuesta tampoco lo delate. Con el correo apagado no hace nada (y el GET lo dice).
_forgot_log = logging.getLogger("monitor.mail")


def _mandar_reset_en_background(email: str, nombre: str, username: str, link: str) -> None:
    try:
        send_mail(email, *mail_reset(nombre, username, link, 60, None))
    except Exception as e:   # noqa: BLE001 — nunca llega al cliente
        _forgot_log.warning("forgot: no se pudo mandar el mail a %s: %s", _limpio(username), type(e).__name__)


@router.get("/forgot", response_class=HTMLResponse)
def forgot_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "pages/forgot.html",
                                       {"sin_mail": not settings.mail_enabled})


@router.post("/forgot", response_class=HTMLResponse)
def forgot_submit(request: Request, background_tasks: BackgroundTasks, dato: str = Form(""),
                  db: Session = Depends(get_db)):
    ip = _client_ip(request)
    clave = (normalizar_email(dato) or "").strip().lower()
    if _rate_limited(_forgot_attempts_ip, ip, *_FORGOT_IP) or (
            clave and _rate_limited(_forgot_attempts_dato, clave, *_FORGOT_DATO)):
        _audit.info("auth forgot=ratelimited ip=%s", _limpio(ip), extra={"console": True})
        return _TEMPLATES.TemplateResponse(request, "pages/forgot_enviado.html",
                                           {"ratelimited": True}, status_code=429)
    if settings.mail_enabled and clave:
        user = db.query(UserORM).filter(UserORM.username == dato.strip()).first()
        if user is None and "@" in clave:
            user = db.query(UserORM).filter(UserORM.email == clave).first()
        if user is not None and user.is_active and user.email and user.hashed_password != SIN_PASSWORD_HASH:
            token = reset_service.issue_reset_token(db, user, purpose="reset", channel="self", by=None)
            link = reset_service.reset_link(request, token)
            nombre = (user.full_name or user.username).split()[0]
            background_tasks.add_task(_mandar_reset_en_background, user.email, nombre, user.username, link)
            _audit.info("auth forgot=requested target=%s ip=%s", _limpio(user.username), _limpio(ip),
                        extra={"console": True})
            return _TEMPLATES.TemplateResponse(request, "pages/forgot_enviado.html", {})
    _audit.info("auth forgot=noop ip=%s", _limpio(ip), extra={"console": True})
    return _TEMPLATES.TemplateResponse(request, "pages/forgot_enviado.html", {})
```

(Import `SIN_PASSWORD_HASH` de `core.security`: un invitado que nunca eligió clave NO recibe reseteo por autoservicio — su link lo maneja el admin. `normalizar_email` ya está importado desde la Task 1 de F2.)

- [ ] **Step 4: Templates.** `forgot.html`:

```html
{% extends "base_public.html" %}
{% block title %}Recuperar acceso - Monitor Renta Fija AR{% endblock %}
{% block header_meta %}Recuperar acceso{% endblock %}
{% block content %}
<div class="pub-card">
  <h2>Recuperar acceso</h2>
  {% if sin_mail %}
  <p class="sub">El envío de mails no está habilitado en este servidor. Pedile el link a tu administrador: te lo puede generar desde el Manager.</p>
  <div class="links"><a href="/login">Volver al ingreso</a></div>
  {% else %}
  <p class="sub">Ingresá tu usuario o tu email. Si hay una cuenta, te mandamos un link para elegir una contraseña nueva.</p>
  <form method="POST" action="/forgot" autocomplete="off">
    <label class="fld"><span>Usuario o email</span>
      <input type="text" name="dato" required maxlength="254" autofocus autocomplete="username" placeholder="mcaceres o m.caceres@…"></label>
    <button type="submit">Enviarme el link</button>
  </form>
  <div class="links"><a href="/login">Volver al ingreso</a></div>
  {% endif %}
</div>
{% endblock %}
```

`forgot_enviado.html`:

```html
{% extends "base_public.html" %}
{% block title %}Recuperar acceso - Monitor Renta Fija AR{% endblock %}
{% block header_meta %}Recuperar acceso{% endblock %}
{% block content %}
<div class="pub-card">
  <h2>Revisá tu correo</h2>
  {% if ratelimited %}
  <p class="sub">Demasiados pedidos desde tu conexión o para ese dato. Esperá unos minutos y volvé a intentar; si ya te llegó un mail, usá ese link.</p>
  {% else %}
  <p class="sub">Si existe una cuenta con ese dato, en unos minutos vas a recibir un mail con el link. Vence en 60 minutos y sirve una sola vez.</p>
  <p class="sub">¿No llegó? Mirá en spam o pedile el link a tu administrador.</p>
  {% endif %}
  <div class="links"><a href="/login">Volver al ingreso</a></div>
</div>
{% endblock %}
```

`login.html`: después del `<button type="submit">Ingresar</button>` agregar `<div class="links"><a href="/forgot">¿Olvidaste tu contraseña?</a></div>`. `reset_invalido.html`: cambiar el `.links` por `<div class="links"><a href="/forgot">Pedir un link nuevo</a> · <a href="/login">Volver al ingreso</a></div>`.

- [ ] **Step 5: GREEN** — `tests/test_users_manager.py tests/test_aud_G_tests_route_auth.py tests/test_auth.py tests/test_sec_csrf_y_headers.py` + ruff. **Mutación**: en `forgot_submit` cambiar la respuesta del `noop` a otra plantilla → `test_forgot_responde_igual...` ROJO; restaurar.
- [ ] **Step 6: Commit** — "Autoservicio /forgot: link en el login, respuesta neutra, envío en background y rate-limit doble".

---

### Task 4: nginx, docs de despliegue y cierre

**Files:** `deploy/nginx/monitores.conf`, `docs/despliegue.md`, `.claude/rules/auth.md`, `docs/auth.md`, `CLAUDE.md` (Web: `/forgot`), `docs/decisiones.md` (D5: mail configurado por env), spec §9 (F3 hecha).

- [ ] **Step 1: nginx** — después de `location = /login { … }`:

```nginx
    # Autoservicio de contraseña y consumo del link: misma zona que el login (POST-only).
    location = /forgot {
        limit_req zone=login burst=5 nodelay;
        proxy_pass http://127.0.0.1:8000;
    }
    location ~ ^/reset/ {
        limit_req zone=login burst=5 nodelay;
        proxy_pass http://127.0.0.1:8000;
    }
```

Correr `tests/test_ops_deploy_config.py` (fija `proxy_set_header Host` a nivel server y la zona `login`).

- [ ] **Step 2: `docs/despliegue.md`** — en "Lo que hay que setear en prod", bullet nuevo:

```markdown
- **Correo saliente (Manager: reseteo por mail, invitaciones, «¿Olvidaste tu contraseña?»)**:
  `MONITOR_SMTP_HOST=smtp.gmail.com`, `MONITOR_SMTP_PORT=587`, `MONITOR_SMTP_USER=<tu cuenta gmail>`,
  `MONITOR_SMTP_PASSWORD=<contraseña de aplicación>` y opcional `MONITOR_SMTP_FROM`. Van en el `.env`
  del servidor (mismo lugar que las credenciales BYMA), nunca en el repo. Host vacío = correo
  apagado: el Manager esconde «Enviar link por mail» y `/forgot` avisa que pidan el link al
  administrador. **Contraseña de aplicación de Gmail**: la cuenta necesita verificación en 2 pasos;
  después en `myaccount.google.com › Seguridad › Contraseñas de aplicaciones` se crea una para
  «Monitor» (16 caracteres, se muestra una sola vez). Verificar egress a 587 desde OCI en el primer
  deploy (`ssh monitor-oci 'timeout 5 bash -c "</dev/tcp/smtp.gmail.com/587" && echo abierto'`); si
  está cerrado, abrir la regla de salida en la security list. `MONITOR_PUBLIC_URL=http://129.80.148.166`
  hace explícita la base de los links (sin ella se usa el Host del request, que nginx reenvía).
```

- [ ] **Step 3: Rules/docs** — `.claude/rules/auth.md`: rutas públicas + `/forgot`; bullets: canal `mail` (dentro del request, con link de respaldo), invitación por mail, `/forgot` (neutra, background, 3/IP/15 min y 3/dato/60 min, invitados excluidos), `mailer.py` (STARTTLS, timeout 15 s, `MailNotConfigured`), `settings.mail_enabled`. `docs/auth.md`: párrafo "Correo y autoservicio" con lo mismo. `CLAUDE.md` Web: `(/login, /logout, /api/health, /reset/{token}, /forgot, /static)`. `docs/decisiones.md` D5: agregar "Correo por env en el server; sin SMTP la feature degrada al link copiable". Spec §9: marcar F1-F3 hechas con fecha.

- [ ] **Step 4: Gate completo** (ruff + pytest) y commit "Docs y nginx: correo saliente, /forgot y rate-limit de /reset".
- [ ] **Step 5 (controller):** `/security-review`, smoke en :8001 con `MONITOR_SMTP_HOST` vacío (botón deshabilitado, `/forgot` sin formulario) y con un SMTP falso (stub) si se puede, review final, merge, push, deploy con OK de David, y **configurar el `.env` de prod lo hace David** (contraseña de aplicación).

## Self-review

- Cobertura F3 (§9): mailer + settings (T1), canal mail + invitación por mail (T2), `/forgot` + link en login + rate-limits (T3), nginx + docs de despliegue + rules (T4). §10 riesgos: egress 587 y Gmail app password documentados en T4.
- Sin placeholders: código y tests completos; los textos de los mails están escritos.
- Consistencia: `send_mail(to, subject, text, html=None)` en T1/T2/T3; `mail_reset(nombre, username, link, minutos, por)` y `mail_invitacion(nombre, username, link, horas, por)` devuelven `(asunto, texto, html)` y se desempacan con `*`; `settings.mail_enabled` en T1/T2/T3; buckets `_forgot_attempts_ip/_dato` y `_rate_limited(bucket, key, max, window)` (F2) en T3; `_PUBLIC_PATHS` con `/forgot` en T3.
