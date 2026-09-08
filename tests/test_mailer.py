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
    monkeypatch.setattr(settings, "public_url", "http://testserver")   # mail_enabled exige las dos
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


def test_mail_enabled_exige_host_y_public_url_y_mail_link_solo_usa_public_url(monkeypatch):
    """Sin URL pública no hay forma segura de armar el link de un mail (el Host de un request
    anónimo lo elige quien lo manda: reset poisoning): el correo queda APAGADO aunque haya
    SMTP, y `mail_link` se niega a armar un link en vez de caer a `request.base_url`."""
    from apps.web import reset_service
    from core.infrastructure.mailer import MailNotConfigured
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "public_url", "http://x.test/")
    assert settings.mail_enabled is False
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "public_url", "")
    assert settings.mail_enabled is False
    with pytest.raises(MailNotConfigured):
        reset_service.mail_link("abc")
    monkeypatch.setattr(settings, "public_url", "http://x.test/")
    assert settings.mail_enabled is True
    assert reset_service.mail_link("abc") == "http://x.test/reset/abc"


def test_settings_denuncia_por_error_smtp_sin_public_url(caplog):
    """`MONITOR_SMTP_HOST` seteado y `MONITOR_PUBLIC_URL` vacío: la app arranca con el correo
    apagado (fail-closed) y lo dice por ERROR nombrando la variable que falta; con las dos
    seteadas no hay denuncia."""
    import logging
    from config.settings import Settings
    with caplog.at_level(logging.ERROR, logger="config.settings"):
        s = Settings(smtp_host="smtp.test", public_url="")
    assert s.mail_enabled is False
    errores = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("MONITOR_PUBLIC_URL" in m for m in errores), errores
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="config.settings"):
        assert Settings(smtp_host="smtp.test", public_url="http://x.test").mail_enabled is True
    assert not [r for r in caplog.records if "MONITOR_PUBLIC_URL" in r.getMessage()]


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
    ctx = s.llamadas[0][1]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True, "TLS se verifica SIEMPRE"
    assert s.llamadas[1][1:] == ("monitor@ejemplo.com", "app-password")
    msg = s.llamadas[2][1]
    assert msg["To"] == "m.caceres@ejemplo.com" and msg["From"] == "monitor@ejemplo.com" and msg["Subject"] == "Asunto"
    assert msg.get_body(preferencelist=("plain",)).get_content().strip() == "texto plano"
    assert "<p>html</p>" in msg.get_body(preferencelist=("html",)).get_content()


def test_send_mail_sin_usuario_no_hace_login_y_respeta_el_remitente_visible(smtp_config, monkeypatch):
    """Un relay sin autenticación (`smtp_user=""`) no manda `login`; `smtp_from` es el
    remitente visible tal cual (nombre + dirección)."""
    import smtplib
    from core.infrastructure import mailer
    _FakeSMTP.instancias.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_from", "Monitor <monitor@dominio.test>")
    mailer.send_mail("a@b.co", "x", "y")
    s = _FakeSMTP.instancias[-1]
    assert [c[0] for c in s.llamadas] == ["starttls", "send", "quit"]
    assert s.llamadas[1][1]["From"] == "Monitor <monitor@dominio.test>"


def test_asend_mail_corre_send_mail_en_otro_hilo_con_los_mismos_args(monkeypatch):
    """`asend_mail` es `to_thread(send_mail, …)`: mismos argumentos, otro hilo (SMTP es
    bloqueante y no puede frenar el event loop)."""
    import asyncio
    import threading
    from core.infrastructure import mailer
    visto = {}

    def fake(to, subject, text, html=None):
        visto.update(hilo=threading.get_ident(), args=(to, subject, text, html))
    monkeypatch.setattr(mailer, "send_mail", fake)
    asyncio.run(mailer.asend_mail("a@b", "s", "t"))
    assert visto["args"] == ("a@b", "s", "t", None)
    assert visto["hilo"] != threading.get_ident(), "tiene que correr fuera del hilo del loop"


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
    a2, t2, _ = mail_invitacion("Juan", "jperez", link, 72, "dberisso")
    assert "72 horas" in t2 and "dberisso, administrador" in t2
    _, t3, _ = mail_invitacion("Juan", "jperez", link, 72, None)
    assert "dberisso" not in t3 and "Un administrador" in t3
