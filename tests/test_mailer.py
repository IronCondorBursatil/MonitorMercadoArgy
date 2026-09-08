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
