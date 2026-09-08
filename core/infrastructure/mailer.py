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
    """El correo está apagado a propósito: `settings.smtp_host` o `settings.public_url`
    vacíos (`settings.mail_enabled` exige las dos; sin URL pública no hay link seguro)."""


def send_mail(to: str, subject: str, text: str, html: Optional[str] = None) -> None:
    if not settings.mail_enabled:
        raise MailNotConfigured("MONITOR_SMTP_HOST o MONITOR_PUBLIC_URL vacíos: el correo está desactivado")
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
