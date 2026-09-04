#!/bin/bash
# Instala la configuración de sistema VERSIONADA (unit de systemd, sitio de nginx,
# journald, profile.d, sudoers) desde `deploy/` hacia /etc.
#
#   sudo bash deploy/bin/install-config.sh            # muestra el diff y pregunta
#   sudo bash deploy/bin/install-config.sh --apply    # instala
#
# POR QUÉ ESTO NO LO HACE `deploy.sh`: instalar un unit de systemd es
# root-equivalente (un unit puede declarar `User=root`), así que dárselo con NOPASSWD
# a la automatización sería conceder root disfrazado. `deploy.sh` sólo DETECTA el
# drift y avisa; aplicarlo es una decisión de un humano con root. Ver
# `deploy/sudoers.d/monitor`.
set -euo pipefail

cd "$(dirname "$0")/../.."
REPO="$(pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "!!! Corré esto como root: sudo bash deploy/bin/install-config.sh [--apply]" >&2
    exit 1
fi

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

# origen                                    destino                                              modo
MAPA="
deploy/systemd/monitores.service            /etc/systemd/system/monitores.service                644
deploy/systemd/journald-monitor.conf        /etc/systemd/journald.conf.d/monitor.conf            644
deploy/nginx/monitores.conf                 /etc/nginx/sites-available/monitores                 644
deploy/profile.d/monitor.sh                 /etc/profile.d/monitor.sh                            644
deploy/sudoers.d/monitor                    /etc/sudoers.d/monitor                               440
"

CAMBIOS=0
while read -r src dst modo; do
    [ -z "${src:-}" ] && continue
    if [ ! -f "$REPO/$src" ]; then
        echo "  !! falta $src en el repo"; continue
    fi
    if [ -f "$dst" ] && diff -q "$REPO/$src" "$dst" >/dev/null 2>&1; then
        printf "  =  %s\n" "$dst"
        continue
    fi
    CAMBIOS=1
    printf "  ~  %s\n" "$dst"
    if [ "$APPLY" = "0" ]; then
        diff -u "$dst" "$REPO/$src" 2>/dev/null | sed 's/^/       /' | head -30 || true
        continue
    fi

    # El sudoers se valida ANTES de instalarse: un archivo invalido ahi deja al
    # usuario sin sudo y la unica recuperacion es la consola serial de OCI.
    if [ "$dst" = "/etc/sudoers.d/monitor" ]; then
        if ! visudo -c -f "$REPO/$src" >/dev/null; then
            echo "  !! el sudoers NO valida — no se instala" >&2
            exit 1
        fi
    fi
    mkdir -p "$(dirname "$dst")"
    install -m "$modo" -o root -g root "$REPO/$src" "$dst"
    printf "     instalado\n"
done <<< "$MAPA"

if [ "$APPLY" = "0" ]; then
    [ "$CAMBIOS" = "1" ] && echo && echo "== DRY RUN. Para aplicar: sudo bash deploy/bin/install-config.sh --apply ==" \
                         || echo "  (nada que instalar)"
    exit 0
fi

echo
echo "=== recargando ==="
systemctl daemon-reload && echo "  systemd: daemon-reload"
if nginx -t 2>/dev/null; then
    ln -sf /etc/nginx/sites-available/monitores /etc/nginx/sites-enabled/monitores
    rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-enabled/monitor
    nginx -t && systemctl reload nginx && echo "  nginx: recargado"
else
    echo "  !! nginx -t FALLÓ: no se recarga (la config vieja sigue viva)" >&2
    exit 1
fi
systemctl restart systemd-journald && echo "  journald: reiniciado"
echo
echo "Falta reiniciar la app para tomar el unit nuevo:  sudo systemctl restart monitores"
