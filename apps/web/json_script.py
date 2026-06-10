"""json_for_script: serialización JSON segura para embeber en un <script>.

Escapa `<` `>` `&` y los separadores de línea U+2028/U+2029 a `\\uXXXX` — siguen
siendo JSON válido (JSON.parse y los literales JS los parsean al MISMO valor) pero
ya no pueden cerrar el tag (`</script>`) ni romper el parser JS (mitiga XSS, S4).

Usarlo en TODO router que embeba JSON en un template con `|safe` (layout del
dashboard, charts/share de paneles, futuros, chain/smile de opciones). Un
`json.dumps` pelado ahí es un breakout esperando un string user-influenced —
hay un test que lo vigila (test_json_script.test_all_embed_sites_use_the_shared_helper).
"""

from __future__ import annotations

import json

# char → escape JSON inerte en HTML/JS. Los dos últimos son los separadores de
# línea Unicode, válidos en JSON pero ilegales en literales JS pre-ES2019.
_REPLACEMENTS = (
    ("<", "\\u003c"),
    (">", "\\u003e"),
    ("&", "\\u0026"),
    (" ", "\\u2028"),
    (" ", "\\u2029"),
)


def json_for_script(obj) -> str:
    """json.dumps con escapes seguros para contexto <script>."""
    out = json.dumps(obj, ensure_ascii=False)
    for ch, esc in _REPLACEMENTS:
        out = out.replace(ch, esc)
    return out
