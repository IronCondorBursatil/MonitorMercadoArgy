# `.claude/` — configuración versionada del agente

Qué hay acá y por qué (detalle y fuentes en `agents.md › §0.5` y `§0.8 Fase 2`):

- **`settings.json`** (versionado): permisos y hooks del **proyecto**. Se mergea con
  `~/.claude/settings.json` (global, personal) y con `settings.local.json` (personal,
  gitignoreado). Orden de evaluación **deny → ask → allow**, sin importar la especificidad
  ni el archivo: un `ask` de acá prompta aunque el global o el local tengan un `allow` más
  amplio. Las reglas duras viven en `deny`/`ask`; los hooks son la segunda capa
  (best-effort). Cada regla crítica está dos veces porque una regla `Bash(...)` **no** cubre
  la tool PowerShell. `Edit(path)` cubre Edit, Write y NotebookEdit; una regla `Write(path)`
  se acepta pero nunca se consulta (no usar). JSON no admite comentarios, por eso este README.
- **`hooks/guard.py`**: hook `PreToolUse` en forma exec (`py -3.12 guard.py`), para lo que un
  patrón de permisos no expresa: force-push por argv (`--force`, `-f`, refspec `+`),
  escrituras por shell a `apps/web/static/js/on.js` (autogenerado), lectura de `.env` /
  `jwt_secret` por cmdlets de PowerShell, bypass de TLS (`curl -k`, `-SkipCertificateCheck`).
  Responde JSON `permissionDecision` deny/ask con exit 0, o exit 0 sin salida (sin opinión).
  Tests: `tests/test_claude_guard.py`. **Cuidado con el orden**: en forma exec, si
  `guard.py` no existe es `python` el que sale con exit 2 y eso **bloquea todo Bash y
  PowerShell** de la sesión (pasó el 2026-09-07: settings escrito antes que el script).
  Primero el script, después el settings; y tras tocar cualquiera de los dos, probar con
  una llamada deliberada (`Get-Content .env` debe dar deny; `git status` debe pasar).
- **`skills/<nombre>/SKILL.md`**: `/gate`, `/smoke`, `/deploy` (solo usuario), `/compound`,
  `/deps-refresh` (solo usuario). Frontmatter según la doc oficial: `description` es lo único
  que Claude ve al inicio; `allowed-tools` **pre-aprueba** durante el turno, no restringe.
- **`settings.local.json`** (ignorado): lo personal de esta máquina. No poner acá `allow`
  amplios: un `ask` del `settings.json` del proyecto igual gana.

Verificación en una sesión nueva: `/hooks` (lista los hooks y su archivo de origen),
`/permissions` (reglas y origen; el warning por nombre de tool inválido sale al arrancar),
`/context` (qué se cargó).
