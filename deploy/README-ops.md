# Operación del servidor

Runbooks de la instancia de producción. Complementa la sección **Despliegue** de
`CLAUDE.md`; acá va lo que se hace *en la caja*.

| | |
|---|---|
| host | `monitor-oci` (alias SSH) — Oracle Cloud, `VM.Standard.A1.Flex` 4 OCPU / 24 GB, **ARM aarch64** |
| SO | Ubuntu 24.04.4, Python 3.12.3 |
| usuario | `ubuntu` (el servicio NO corre como root) |
| repo | `/home/ubuntu/MonitorMercadoArgy`, clonado de GitHub |
| datos | `/var/lib/monitor` (`MONITOR_DB_DIR`) — **fuera del working tree**, invariante |
| web | nginx :80 → uvicorn 127.0.0.1:8000 |

## Deploy

```bash
ssh monitor-oci
cd MonitorMercadoArgy && bash deploy.sh
```

`deploy.sh` hace `git pull origin main`, valida/crea el venv 3.12, instala
`requirements.txt`, reinicia el servicio y verifica `/api/health`. **No** corre tests
ni migraciones ni instala configuración del sistema (ver abajo).

El gate en la misma arquitectura que produce:

```bash
bash scripts/check.sh --install-dev     # ruff + pytest en el ARM
```

## Configuración del sistema (systemd, nginx, journald, sudoers)

Vive versionada en `deploy/` y se instala **a mano, con root**, nunca desde el deploy:

```bash
sudo bash deploy/bin/install-config.sh            # muestra el diff
sudo bash deploy/bin/install-config.sh --apply    # instala + recarga
sudo systemctl restart monitores                  # para tomar un unit nuevo
```

**Por qué no lo hace `deploy.sh`**: instalar un unit de systemd es root-equivalente
(un unit puede declarar `User=root` + `ExecStart=/bin/sh -c …`). Dárselo con NOPASSWD
a la automatización sería conceder root disfrazado. Por eso `deploy.sh` sólo detecta
el drift y avisa.

El instalador valida el `sudoers` con `visudo -c` **antes** de escribirlo: un archivo
inválido ahí deja la única recuperación en la consola serial de OCI.

### Pendiente: activar el sudoers acotado

`deploy/sudoers.d/monitor` ya está instalado y concede sólo lo que necesita la
automatización (reiniciar el servicio, recargar nginx). Pero **cloud-init deja un
`ubuntu ALL=(ALL) NOPASSWD:ALL`** en `/etc/sudoers.d/90-cloud-init-users` que lo
vuelve irrelevante.

Sacarlo tiene un requisito previo: hoy `ubuntu` tiene la **contraseña bloqueada**
(`passwd -S ubuntu` → `L`), así que sin el `NOPASSWD:ALL` no queda ninguna vía para
escalar a root — ni para instalar un paquete ni para correr `install-config.sh`.

Secuencia segura, con red:

```bash
sudo passwd ubuntu                      # 1. darle una contraseña (va al gestor de claves)
sudo systemd-run --on-active=300 --unit=sudo-rescue \
    /bin/cp /etc/sudoers.d/90-cloud-init-users.bak /etc/sudoers.d/90-cloud-init-users   # 2. red de 5 min
sudo cp /etc/sudoers.d/90-cloud-init-users{,.bak}
sudo sh -c '> /etc/sudoers.d/90-cloud-init-users'                                       # 3. desactivar
sudo -l                                  # 4. verificar que quedan los 7 comandos + (ALL:ALL) ALL con password
sudo systemctl restart monitores         # 5. probar el camino de la automatización
sudo systemctl stop sudo-rescue.timer    # 6. sólo si todo lo anterior anduvo
```

Si algo falla, no toques nada: a los 5 minutos la red restaura la regla vieja.

## Backups

`backup_db` toma un snapshot online de `catalog.db` (1×/día al arrancar y en cada
vuelta horaria del `_price_history_loop`) en `/var/lib/monitor/backups`, rotando 7.

**Sigue pendiente el backup fuera de la caja** (plan §1.3): los 4 históricos
(`price_history`, `fci_history`, `ratings_history`, `index_history`), el `jwt_secret`,
el `.env` y `cartera.json` **no se respaldan nunca**, y todo vive en el mismo disco.
Con una instancia Always Free que Oracle puede reclamar por inactividad, eso es la
brecha más grande que queda.

## Diagnóstico

```bash
systemctl status monitores
journalctl -u monitores -f                 # WARNING+ y access 4xx/5xx
journalctl -u monitores -p warning -n 50
curl -s localhost:8000/api/health | jq     # directo, sin nginx
curl -s localhost/api/health | jq          # a través de nginx
systemd-analyze security monitores.service # hardening (objetivo: ≤ 5)
sudo fail2ban-client status sshd
```

`/api/health` es **público** (probe externo) y por eso va recortado: cuentas,
frescura, nombres de loops caídos y `loop_crashes_24h`. El detalle del error está en
`/health/badge`, detrás de login.

## Scripts manuales

**Siempre con `MONITOR_DB_DIR` explícito**: el drop-in de systemd no lo hereda una
shell interactiva, y sin él los scripts resuelven `db_dir` al default vacío. Hay un
`/etc/profile.d/monitor.sh` que lo exporta en shells de login, pero no confíes en él
para un `ssh host 'comando'` (no es login shell):

```bash
cd /home/ubuntu/MonitorMercadoArgy
MONITOR_DB_DIR=/var/lib/monitor venv/bin/python scripts/<x>.py
```

- `backfill_tamar_anchor.py` — imprime contra qué base corre y aborta si está vacía.
  **Exige el servicio parado** (`guard_write`).
- `bench_pricing.py` — exige el servicio parado (verifica que no se escribió nada).
- `bench_churn.py` — corre con la app viva, pero **sólo sirve en rueda**
  (11:00-17:00 ART, día hábil): fuera de horario da 0% de churn y el número engaña.

## Notas de la caja

- **ARM**: todas las wheels (numpy/scipy/pandas/matplotlib) son `aarch64` y entran sin
  compilar. `OPENBLAS_NUM_THREADS=1` en el unit — el código es elementwise, no llama
  BLAS, y 4 threads ociosos por proceso ensucian el p95 de CPU que Oracle mira.
- **FastAPI**: el servidor instala de `requirements.txt` (abierto) y la laptop de
  `requirements.lock` (pinneado). Divergieron: 0.141 vs 0.136, y `include_router`
  cambió de forma. Ver `tests/_routes.py`.
- **Reclamo por inactividad**: Oracle puede reclamar una instancia Always Free si en
  7 días CPU p95 < 20% **y** red < 20% **y** RAM < 20%. La caja cumple los tres
  (CPU 4%, RAM 4,3%). No hay mitigación honesta sin un generador de carga; la salida
  real es pasar la cuenta a Pay-As-You-Go o aceptar el riesgo con backups fuera.
