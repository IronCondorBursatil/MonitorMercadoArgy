"""Textos de los mails del Manager (reseteo e invitación). Puros: (asunto, texto, html).
El HTML es inline y no carga NADA externo (ni imágenes ni CSS): lo que ve el cliente de
correo es lo que está acá. El link aparece como botón y como URL en claro."""

from __future__ import annotations

from html import escape
from typing import Optional

_PIE = "Monitor Renta Fija AR · mail automático, no respondas a esta dirección."


def _html(saludo: str, cuerpo: str, boton: str, link: str, nota: str, aviso: str) -> str:
    """`cuerpo`, `nota` y `aviso` llegan YA seguros (constantes nuestras o escapados por el
    llamador); `saludo`, `boton` y `link` se escapan acá."""
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;line-height:1.55;'
        'color:#1f2328;max-width:560px;margin:0 auto;padding:24px">'
        f'<p style="margin:0 0 12px">{escape(saludo)}</p>'
        f'<p style="margin:0 0 18px">{cuerpo}</p>'
        f'<p style="text-align:center;margin:0 0 18px"><a href="{escape(link)}" '
        'style="display:inline-block;background:#2962ff;color:#fff;font-weight:600;padding:11px 22px;'
        f'border-radius:6px;text-decoration:none">{escape(boton)}</a></p>'
        f'<p style="margin:0 0 6px;color:#57606a;font-size:13px">{nota}</p>'
        f'<p style="margin:0 0 18px;color:#57606a;font-size:13px">{aviso}</p>'
        '<p style="margin:0;color:#57606a;font-size:12px">Si el botón no funciona, copiá esta dirección en '
        f'el navegador:<br><span style="font-family:Consolas,monospace;word-break:break-all">{escape(link)}</span></p>'
        f'<p style="margin:18px 0 0;color:#8b949e;font-size:11px">{escape(_PIE)}</p></div>'
    )


def mail_reset(nombre: str, username: str, link: str, minutos: int, por: Optional[str]) -> tuple[str, str, str]:
    quien = f"{por}, administrador del Monitor," if por else "Vos (o alguien con tu usuario)"
    asunto = "Tu link para elegir una contraseña nueva — Monitor Renta Fija AR"
    aviso = "Si no lo pediste, ignorá este mail: tu contraseña actual sigue igual."
    texto = (
        f"Hola {nombre},\n\n"
        f"{quien} pidió restablecer la contraseña de tu usuario {username} en el Monitor.\n"
        f"Entrá al link y elegí una nueva:\n\n{link}\n\n"
        f"El link vence en {minutos} minutos y sirve una sola vez.\n"
        f"{aviso}\n\n"
        f"{_PIE}\n"
    )
    html = _html(f"Hola {nombre},",
                 f"{escape(quien)} pidió restablecer la contraseña de tu usuario <b>{escape(username)}</b> en el "
                 "Monitor. Entrá al link y elegí una nueva:",
                 "Elegir contraseña nueva", link,
                 f"El link vence en <b>{minutos} minutos</b> y sirve una sola vez.", aviso)
    return asunto, texto, html


def mail_invitacion(nombre: str, username: str, link: str, horas: int, por: Optional[str]) -> tuple[str, str, str]:
    quien = f"{por}, administrador del Monitor," if por else "Un administrador del Monitor"
    asunto = "Te invitaron al Monitor Renta Fija AR"
    aviso = "Si no lo esperabas, ignorá este mail."     # un invitado no tiene "contraseña actual"
    texto = (
        f"Hola {nombre},\n\n"
        f"{quien} te creó el usuario {username} en el Monitor Renta Fija AR.\n"
        f"Entrá al link y elegí tu contraseña:\n\n{link}\n\n"
        f"El link vence en {horas} horas y sirve una sola vez.\n"
        f"{aviso}\n\n"
        f"{_PIE}\n"
    )
    html = _html(f"Hola {nombre},",
                 f"{escape(quien)} te creó el usuario <b>{escape(username)}</b> en el Monitor Renta Fija AR. "
                 "Entrá al link y elegí tu contraseña:",
                 "Elegir mi contraseña", link,
                 f"El link vence en <b>{horas} horas</b> y sirve una sola vez.", aviso)
    return asunto, texto, html
