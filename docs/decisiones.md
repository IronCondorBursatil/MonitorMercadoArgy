# Decisiones — registro de las que son de David, con la recomendación del agente

Formato: **decisión · recomendación · estado · consecuencias**. El agente plantea; David decide.
Cuando una decisión se toma, se anota acá la fecha y quién la tomó, y se aplica en la fase
que corresponde. Ver `agents.md › §0.8 Fase 0`.

## D1 · Mudar el repo fuera de OneDrive (p. ej. `C:\dev\monitor`)

- **Recomendación**: **sí**. OneDrive sincroniza `.git`, `.superpowers/`, los watchers de
  pyright y cualquier venv; git + GitHub ya son el backup. El límite de 5 MB por archivo de
  OneDrive es el motivo por el que no hay venv en el proyecto.
- **Estado**: **PENDIENTE de David** (no lo puede hacer el agente: requiere cerrar sesiones y
  mover la carpeta).
- **Procedimiento seguro** (verificado contra la doc de Claude Code, 2026-09-07): la memoria
  auto, los transcripts y el entry de trust/MCP en `~/.claude.json` están atados al *slug* de
  la ruta (`c--Users-david-OneDrive-Monitores---Data912`); mover la carpeta los deja huérfanos.
  1. Con Claude Code cerrado, agregar a `.claude/settings.local.json` (o al global):
     `"autoMemoryDirectory": "C:/Users/david/.claude/memory-monitores"` y **copiar** ahí el
     contenido de `~/.claude/projects/c--Users-david-OneDrive-Monitores---Data912/memory/`.
  2. Mover la carpeta a `C:\dev\monitor`.
  3. Abrir Claude Code ahí, aceptar el trust dialog; `.claude/settings.local.json` viaja solo
     porque vive dentro del árbol.
  4. Opcional: renombrar `~/.claude/projects/<slug-viejo>/` al slug nuevo
     (`C--dev-monitor`) para conservar los transcripts; `claude --resume <id>` los encuentra
     igual cross-project (v2.1.223+).
- **Consecuencias si no se hace**: nada rompe hoy; el riesgo es OneDrive pisando `.git` en
  una sincronización y `.superpowers/`/`.playwright-mcp/` subiendo basura a la nube.

## D2 · Política de deploy respecto de las versiones

- **A** (recomendada): `deploy.sh --upgrade` instala dentro de las cotas de
  `requirements.txt`, así prod converge a la resolución que el CI validó ese día; con freeze
  antes y después, el rollback es `pip install -r <freeze-anterior>`.
- **B**: mantener el default actual (sin `--upgrade`: prod solo cambia de versiones cuando el
  venv se recrea) y agregar `deploy.sh --rebuild` explícito.
- **Estado**: **PENDIENTE de David**. Mientras tanto la Fase 1 implementa el mecanismo sin
  cambiar el comportamiento por default: `deploy.sh` sigue instalando sin `--upgrade`, gana
  el flag `--upgrade` (opción A a demanda) y escribe el freeze antes y después en
  `${MONITOR_DB_DIR:-/var/lib/monitor}/freeze/`. Elegir A = correr `bash deploy.sh --upgrade`.
- **Consecuencias**: con A, un deploy de código puede mover versiones (por eso el freeze);
  con B, prod queda como foto y el drift laptop/CI/prod se reabre en cada rebuild.

## D3 · Rotación de secretos en producción

- **Qué**: el secreto JWT (`/var/lib/monitor/jwt_secret`, migrado tal cual desde el droplet
  el 2026-09-04) y la contraseña del admin por defecto que la memoria marcaba "por rotar".
- **Recomendación**: rotar ambos **antes de la Fase 1** si no se hizo. JWT: borrar el archivo
  y reiniciar el servicio (se regenera solo) o setear `MONITOR_JWT_SECRET_KEY` en el
  `EnvironmentFile`; invalida todas las sesiones (esperado). Admin: desde el ABM de usuarios
  o `scripts/init_admin.py` con `MONITOR_ADMIN_PASSWORD`.
- **Estado**: **HECHA el 2026-09-07** con OK de David ("hacelo" sobre la lista de pendientes).
  El secreto JWT se borró y el restart de `deploy.sh` lo regeneró (archivo nuevo 16:39 UTC;
  todas las sesiones quedaron invalidadas, esperado). La contraseña del `admin` se rotó con
  un script de un solo uso sobre la `catalog.db` de prod (hash nuevo vía
  `core.security.get_password_hash`); la contraseña nueva NO pasó por el chat: quedó en
  `/var/lib/monitor/admin-password-2026-09-07.txt` (0600, owner ubuntu). **David: leerla por
  ssh, guardarla en su gestor y borrar el archivo.**

## D4 · Canal de la alerta de staleness (Fase 3)

- **Recomendación**: email de GitHub por fallo del workflow programado (cero infraestructura
  nueva). GitHub notifica al dueño/último editor del workflow cuando un run falla.
- **Estado**: **adoptada por default por el agente el 2026-09-07** (reversible: es un
  workflow). Límites a conocer: los `schedule` de GitHub corren con demora variable y se
  desactivan solos tras 60 días sin actividad en el repo — un commit cualquiera los reactiva.
- **Alternativa**: cron en el servidor + chequeo de edad dentro de `/smoke`, si el email no
  alcanza.

## D5 · Manager de usuarios v2 y reseteo de contraseña (decidido 2026-09-08)

- **Layout**: Opción A (tabla + ficha en panel lateral HTMX). B (página por usuario) y C
  (tarjetas) descartadas; mockups en el canvas enlazado desde la spec.
- **Correo**: Gmail con contraseña de aplicación, SMTP 587 STARTTLS con `smtplib` (sin
  dependencia nueva). Si molesta el remitente, cambiar `MONITOR_SMTP_*` a un proveedor
  transaccional no toca código. Correo por env en el server; sin SMTP la feature degrada al
  link copiable.
- **HTTP sin dominio**: no hay dominio ni lo va a haber por un largo rato. El link de reseteo
  viaja en claro igual que hoy viaja la contraseña del login; mitigaciones: token de 256 bits
  hasheado, un solo uso, 60 min (invitación 72 h), consumirlo cierra las otras sesiones.
  Camino a HTTPS sin comprar dominio (Let's Encrypt sobre `129-80-148-166.sslip.io`): spike
  aparte, no verificado.
- **Autoservicio** "¿Olvidaste tu contraseña?" sí (respuesta neutra, rate-limit). **Alta por
  invitación** por defecto; contraseña inicial a mano como alternativa.
- Fases: F1 Manager + schema + login (esta rama) · F2 tokens + `/reset/{token}` + link copiable ·
  F3 mailer + `/forgot`.

## Decisiones ya tomadas durante la auditoría (2026-09-07)

- Ramas apiladas in-place en vez de worktree, una por fase (`fase-0-baseline` →
  `fase-1-drift` → …); `main` no se toca hasta que David mergea. Motivo: los hooks y
  permisos de la Fase 2 se prueban en este directorio.
- `gh` se instala en la Fase 0 (no en la 1): la regla de cierre de cada fase lo necesita.
- Fase 5.6 del brief original (copiar los símbolos vivos al motor legacy) **descartada**:
  reemplazada por valores 30/360 a mano en `test_daycount.py` (ver `agents.md §0.6`).
- Fase 3.4: sin endpoint nuevo; se monitorea `/api/health` que ya existe y es público.
