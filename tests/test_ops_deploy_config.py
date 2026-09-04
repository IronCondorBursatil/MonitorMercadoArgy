"""La configuración de sistema (systemd + nginx + sudoers) está VERSIONADA y tiene
propiedades de las que depende el código.

Hasta 2026-09 el unit, el sitio de nginx y el drop-in vivían SÓLO en el servidor:
reconstruir la caja era arqueología y nada impedía que alguien editara el unit a mano
y perdiera el cambio en el próximo rebuild. Ahora viven en `deploy/` y `deploy.sh`
detecta el drift.

Estos tests no prueban que el servidor esté configurado (eso lo verifica
`systemd-analyze security` en la caja): prueban que los archivos versionados
mantengan las propiedades que el resto del sistema asume.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "deploy" / "systemd" / "monitores.service"
NGINX = ROOT / "deploy" / "nginx" / "monitores.conf"
SUDOERS = ROOT / "deploy" / "sudoers.d" / "monitor"


def _txt(p: Path) -> str:
    assert p.is_file(), f"falta {p.relative_to(ROOT)}"
    return p.read_text(encoding="utf-8")


def _efectivo(p: Path) -> str:
    """El archivo SIN comentarios: lo que el sistema realmente lee.

    Estos archivos explican en prosa por qué NO llevan ciertas cosas (una IP
    hardcodeada, un `install` hacia /etc/systemd/system). Un test que busque esos
    strings en el texto crudo se dispara con la explicación — el primer intento de
    este archivo fallaba exactamente así."""
    fuera = []
    for linea in _txt(p).splitlines():
        sin = linea.split("#", 1)[0]
        if sin.strip():
            fuera.append(sin)
    return "\n".join(fuera)


# ── systemd ────────────────────────────────────────────────────────────────
def test_el_unit_no_corre_como_root():
    """El droplet corría como root; la caja nueva no. Un `User=root` acá sería una
    regresión silenciosa que sólo se nota auditando el servidor."""
    assert "User=ubuntu" in _efectivo(UNIT)
    assert "User=root" not in _efectivo(UNIT)


def test_el_unit_saca_las_bases_del_arbol_y_lo_hace_FATAL():
    """`MONITOR_DB_DIR` fuera del working tree es el invariante que evita que un
    `git clean -xfd` se lleve la fuente de verdad. En producción el guard tiene que
    ABORTAR, no sólo loguear: arrancar con la base adentro del árbol es justo el
    estado que un deploy convierte en pérdida de datos."""
    txt = _efectivo(UNIT)
    assert "MONITOR_DB_DIR=/var/lib/monitor" in txt
    assert "MONITOR_DB_IN_TREE_FATAL=true" in txt


def test_el_unit_no_expone_la_app_directo_a_internet():
    """uvicorn escucha en loopback; el único que habla con afuera es nginx."""
    assert "MONITOR_HOST=127.0.0.1" in _efectivo(UNIT)


@pytest.mark.parametrize("directiva", [
    "NoNewPrivileges=true", "PrivateTmp=true", "ProtectKernelTunables=true",
    "ProtectControlGroups=true", "RestrictSUIDSGID=true", "LockPersonality=true",
    "SystemCallArchitectures=native", "CapabilityBoundingSet=",
])
def test_el_unit_conserva_el_hardening(directiva):
    """Fase 1 del hardening: llevó el score de `systemd-analyze security` de
    9.2 UNSAFE a 2.0 OK. Cada directiva que se caiga lo devuelve un poco."""
    assert directiva in _efectivo(UNIT)


def test_el_unit_deja_correr_el_pool_de_procesos_de_opciones():
    """La chain de opciones va a un ProcessPool: necesita AF_UNIX para los pipes y
    /dev/shm para los semáforos de multiprocessing. `PrivateDevices=true` conserva
    /dev/shm; lo que NO puede aparecer es un `PrivateDevices` que lo quite ni un
    deny-list de syscalls que corte clone/execve."""
    txt = _efectivo(UNIT)
    assert "AF_UNIX" in txt, "sin AF_UNIX el ProcessPool no puede comunicarse"
    assert "SystemCallFilter=~" not in txt, "un deny-list de syscalls corta el pool"


def test_el_unit_acota_la_memoria_con_margen():
    """La app usa ~382 MB y el pool sumará ~180 MB. El techo existe para que un
    desmadre pegue contra el cgroup y no contra la caja entera."""
    assert "MemoryMax=4G" in _efectivo(UNIT)


# ── nginx ──────────────────────────────────────────────────────────────────
def test_nginx_manda_el_Host_original():
    """Requisito DURO de la validación de origen (CSRF): se compara `Origin` contra
    el `Host` del request. El default de nginx es `$proxy_host` (127.0.0.1:8000), que
    haría fallar TODO POST de browser. Además tiene que estar a nivel `server`: una
    location que declare cualquier `proxy_set_header` propio descarta los de arriba."""
    txt = _efectivo(NGINX)
    assert "proxy_set_header Host $host;" in txt
    cuerpo_server = txt.split("server {", 1)[1]
    antes_de_location = cuerpo_server.split("location ", 1)[0]
    assert "proxy_set_header Host $host;" in antes_de_location, (
        "`Host` tiene que estar a nivel server para que las location lo hereden")


def test_nginx_no_hardcodea_la_ip():
    """La IP pública ya cambió una vez (efímera → reservada). Con `server_name <ip>`
    —como estaba en el droplet— el sitio deja de responder hasta que alguien edite
    esto a mano."""
    txt = _efectivo(NGINX)
    assert "server_name _;" in txt
    for ip in ("157.230.87.79", "129.213.38.104", "129.80.148.166"):
        assert ip not in txt, f"IP hardcodeada: {ip}"


def test_nginx_no_bufferea_el_SSE():
    """Sin esto nginx retiene los eventos de /stream y los paneles dejan de
    auto-refrescarse — el droplet no lo tenía."""
    assert "proxy_buffering off;" in _efectivo(NGINX)


def test_nginx_limita_el_POST_de_login_pero_no_el_GET():
    """El limiter de la app vive en memoria y se resetea en cada deploy; éste no.
    La clave sale de un `map` sobre el método: throttlear el GET dejaría a un usuario
    sin poder ni ver el formulario."""
    txt = _efectivo(NGINX)
    assert "limit_req_zone" in txt and "zone=login" in txt
    assert "$request_method $login_key" in txt, "la clave tiene que depender del método"
    assert "POST    $binary_remote_addr" in txt or "POST $binary_remote_addr" in txt


def test_nginx_no_publica_su_version():
    assert "server_tokens off;" in _efectivo(NGINX)


# ── sudoers ────────────────────────────────────────────────────────────────
def test_el_sudoers_no_concede_root_disfrazado():
    """Conceder `install` hacia /etc/systemd/system con NOPASSWD es root-equivalente:
    un unit puede declarar `User=root` + `ExecStart=/bin/sh -c ...`. Por eso la
    automatización sólo puede reiniciar servicios, y la instalación de configuración
    es una acción deliberada de un humano con root."""
    txt = _efectivo(SUDOERS)
    assert "NOPASSWD:ALL" not in txt.replace(" ", "")
    assert "/etc/systemd/system" not in txt, (
        "escribir units con NOPASSWD es equivalente a conceder root")
    assert "/etc/nginx" not in txt
    assert "systemctl restart monitores.service" in txt


def test_el_sudoers_alcanza_para_lo_que_hace_el_deploy():
    """Si le falta un verbo, `deploy.sh` se cuelga pidiendo password en un timer."""
    txt = _efectivo(SUDOERS)
    for verbo in ("systemctl restart monitores.service",
                  "systemctl daemon-reload",
                  "systemctl reload nginx"):
        assert verbo in txt, f"falta: {verbo}"


# ── fase 2 del hardening ───────────────────────────────────────────────────
PHASE2 = ROOT / "deploy" / "systemd" / "phase2-protectsystem.conf"


def test_la_fase2_no_se_instala_sola():
    """`ProtectSystem=strict` rompe la app hasta que el log y `cartera.json` salgan del
    working tree (el RotatingFileHandler rota con rename en la raíz del repo). Por eso
    vive en un drop-in aparte que `install-config.sh` NO toca: instalarlo es un paso
    posterior al deploy del código que los mueve."""
    instalador = (ROOT / "deploy" / "bin" / "install-config.sh").read_text(encoding="utf-8")
    assert "phase2" not in instalador, (
        "el instalador aplicaría la fase 2 sin que el código que la habilita esté "
        "desplegado — la app se rompe en la primera rotación de log")


def test_la_fase2_usa_ProtectHome_read_only_y_no_yes():
    """`ProtectHome=yes` haría inaccesible /home/ubuntu, donde vive el repo: el
    servicio no arrancaría. La diferencia entre las dos palabras es que el sitio ande."""
    txt = _efectivo(PHASE2)
    assert "ProtectHome=read-only" in txt
    assert "ProtectHome=yes" not in txt


def test_la_fase2_deja_escribir_el_env():
    """La UI de credenciales BYMA reescribe `.env` en el repo; con el árbol read-only
    hay que exceptuarlo explícitamente o guardar credenciales tira PermissionError."""
    assert ".env" in _efectivo(PHASE2) and "ReadWritePaths" in _efectivo(PHASE2)


# ── el timer del backup (auditoría 2026-09-04) ───────────────────────────────
BACKUP_SVC = ROOT / "deploy" / "systemd" / "monitor-backup.service"
BACKUP_TIMER = ROOT / "deploy" / "systemd" / "monitor-backup.timer"
INSTALADOR = ROOT / "deploy" / "bin" / "install-config.sh"


def test_el_instalador_conoce_el_timer_del_backup():
    """`install-config.sh` es el ÚNICO instalador de configuración de sistema del repo.
    El unit y el timer del backup quedaron fuera de su MAPA: existían en `deploy/`,
    pasaban el gate y nadie los copiaba a /etc. Un backup que nadie instala es
    exactamente la clase de backup que uno cree que tiene."""
    txt = _txt(INSTALADOR)
    for archivo in ("monitor-backup.service", "monitor-backup.timer"):
        assert archivo in txt, f"el instalador no conoce {archivo}"
    assert "enable --now monitor-backup.timer" in txt, (
        "el timer se copia a /etc pero nadie lo habilita: no dispara nunca")


def test_el_unit_del_backup_no_deja_el_bundle_legible_por_cualquiera():
    """El bundle lleva el `jwt_secret`, el `.env` con las credenciales BYMA y los
    hashes de contraseña, y `backup_bundle.py` no hace ningún `chmod`. El unit copiaba
    todo el hardening del servicio principal MENOS `UMask=0077` — justo la directiva
    que gobierna el modo del artefacto que produce."""
    txt = _efectivo(BACKUP_SVC)
    assert "UMask=0077" in txt, "el bundle con los secretos se escribe 0644"
    assert "User=ubuntu" in txt and "User=root" not in txt


def test_el_timer_del_backup_sobrevive_a_la_caja_apagada():
    """`Persistent=true`: sin eso, un reinicio a las 18:00 se come el backup del día
    en silencio."""
    txt = _efectivo(BACKUP_TIMER)
    assert "Persistent=true" in txt
    assert "OnCalendar=" in txt
    assert "WantedBy=timers.target" in txt, "el timer no se puede habilitar"
