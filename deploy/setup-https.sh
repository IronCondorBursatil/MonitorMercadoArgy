#!/bin/bash
# HTTPS para el Monitor (nginx + Let's Encrypt) — se corre EN EL DROPLET, como root.
#
#   bash deploy/setup-https.sh monitor.midominio.com  tu@email.com
#
# REQUISITO QUE NO SE PUEDE SALTEAR: un DOMINIO apuntando por A a la IP del droplet.
# Let's Encrypt NO emite certificados para IPs desnudas (157.230.87.79 no sirve).
# Opciones, de menor a mayor fricción:
#   * DuckDNS (gratis, 5 min): duckdns.org -> subdominio gratis -> apuntá el A a la IP.
#     Queda algo como  mimonitor.duckdns.org
#   * Dominio propio (~USD 10/año): registralo y creá un A -> 157.230.87.79
#   * Cloudflare delante: también necesita dominio, pero te da HTTPS + WAF gratis.
#
# Qué hace:
#   1. Verifica que el dominio resuelva a ESTA máquina (si no, aborta: certbot fallaría).
#   2. Instala certbot y emite el certificado por el desafío HTTP-01 (puerto 80).
#   3. Reescribe el vhost de nginx: 80 redirige a 443, y 443 hace proxy a uvicorn.
#   4. Deja la renovación automática (timer de systemd que ya trae certbot).
#   5. Recuerda activar MONITOR_COOKIE_SECURE=true, que es el punto de todo esto.
set -euo pipefail

DOMINIO="${1:-}"
EMAIL="${2:-}"
APP_PORT="${APP_PORT:-8000}"

if [[ -z "$DOMINIO" || -z "$EMAIL" ]]; then
    echo "uso: bash deploy/setup-https.sh <dominio> <email>"
    echo "ej:  bash deploy/setup-https.sh mimonitor.duckdns.org david@ejemplo.com"
    exit 1
fi
if [[ $EUID -ne 0 ]]; then echo "correlo como root (sudo)"; exit 1; fi

echo ">>> 1/5  Verificando que $DOMINIO apunte a esta máquina..."
IP_PUBLICA="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
IP_DOMINIO="$(getent hosts "$DOMINIO" | awk '{print $1}' | head -1 || true)"
echo "     IP del droplet : ${IP_PUBLICA:-<no se pudo averiguar>}"
echo "     IP del dominio : ${IP_DOMINIO:-<no resuelve>}"
if [[ -z "$IP_DOMINIO" ]]; then
    echo "!!!  $DOMINIO no resuelve. Creá el registro A y esperá la propagación."
    exit 1
fi
if [[ -n "$IP_PUBLICA" && "$IP_DOMINIO" != "$IP_PUBLICA" ]]; then
    echo "!!!  $DOMINIO apunta a $IP_DOMINIO, no a $IP_PUBLICA."
    echo "     Certbot va a fallar el desafío HTTP-01. Corregí el DNS primero."
    exit 1
fi
echo "     OK."

echo ">>> 2/5  Instalando certbot..."
apt-get update -qq
apt-get install -y -qq certbot python3-certbot-nginx

echo ">>> 3/5  Dejando un vhost mínimo para el desafío HTTP-01..."
# certbot --nginx necesita un server con este server_name para validar.
cat > /etc/nginx/sites-available/monitores <<NGINX
server {
    listen 80;
    server_name ${DOMINIO};
    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # SSE (/stream): sin buffering y sin timeout, o el push muere a los 60s.
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 24h;
        chunked_transfer_encoding off;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/monitores /etc/nginx/sites-enabled/monitores
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo ">>> 4/5  Emitiendo el certificado (Let's Encrypt)..."
# --redirect: certbot agrega solo el 301 de 80 -> 443 y el bloque TLS.
certbot --nginx -d "$DOMINIO" --non-interactive --agree-tos -m "$EMAIL" --redirect
nginx -t && systemctl reload nginx

echo ">>> 5/5  Verificando renovación automática..."
systemctl list-timers | grep -i certbot || echo "     (revisá 'systemctl status certbot.timer')"
certbot renew --dry-run

cat <<FIN

============================================================
  HTTPS LISTO en  https://${DOMINIO}
============================================================

FALTA UN PASO, y es el que le da sentido a todo esto:

  1) Activá la cookie Secure (que la sesión NO viaje nunca por HTTP):
       echo 'MONITOR_COOKIE_SECURE=true' >> /etc/monitores/env
       systemctl restart monitores.service

     (si todavía no usás EnvironmentFile, agregá en el unit:
        Environment=MONITOR_COOKIE_SECURE=true )

  2) uvicorn tiene que confiar en los headers del proxy para que
     request.url.scheme sea https. En el ExecStart del servicio:
        --proxy-headers --forwarded-allow-ips='127.0.0.1'

  3) Cerrá el 80 al público si querés (certbot renueva por el 80,
     asi que dejalo abierto o usá --preferred-challenges dns).

FIN
