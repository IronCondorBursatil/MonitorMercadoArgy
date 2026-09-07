"""Tests del hook PreToolUse `.claude/hooks/guard.py` (segunda capa de permisos).

La primera capa son los `permissions.allow/ask/deny` de `.claude/settings.json`, que
cubren `Bash(...)`. El hook existe para lo que un patrón no expresa (agents.md §0.5: el
`if` de los hooks es best-effort, lo duro va en permisos): la tool PowerShell, las
escrituras por shell y el argv de git. Se ejecuta como subproceso, igual que lo hace
Claude Code (forma exec, JSON por stdin, JSON por stdout).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / ".claude" / "hooks" / "guard.py"

ONJS = "apps/web/static/js/on.js"
ONJS_WIN = r"apps\web\static\js\on.js"


def _run(stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def decide(tool_name, tool_input):
    """(returncode, decision | None, reason). Valida la forma del JSON de salida."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": str(ROOT),
    }
    proc = _run(json.dumps(payload))
    assert "Traceback" not in proc.stdout
    if not proc.stdout.strip():
        return proc.returncode, None, ""
    assert proc.stdout.isascii(), "ensure_ascii=True: la consola de Windows es cp1252"
    out = json.loads(proc.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] in ("deny", "ask")
    return proc.returncode, hso["permissionDecision"], hso.get("permissionDecisionReason", "")


def sh(cmd):
    return decide("Bash", {"command": cmd})


def ps(cmd):
    return decide("PowerShell", {"command": cmd})


# --------------------------------------------------------------------------- #
# 1. on.js es AUTO-GENERADO: ninguna escritura, por tool de archivos ni por shell
# --------------------------------------------------------------------------- #
ONJS_FILE_DENY = [
    ("Write", {"file_path": ONJS, "content": "x"}),
    ("Write", {"file_path": ONJS_WIN, "content": "x"}),
    ("Edit", {"file_path": str(ROOT / ONJS_WIN), "old_string": "a", "new_string": "b"}),
    ("Edit", {"file_path": "/srv/app/apps/web/static/js/on.js", "old_string": "a", "new_string": "b"}),
    ("Edit", {"file_path": "C:/x/repo/static/js/on.js", "old_string": "a", "new_string": "b"}),
    ("MultiEdit", {"file_path": ONJS, "edits": []}),
    ("NotebookEdit", {"notebook_path": ONJS, "new_source": ""}),
]


@pytest.mark.parametrize("tool,tool_input", ONJS_FILE_DENY, ids=[f"{t}:{i.get('file_path', i.get('notebook_path'))}" for t, i in ONJS_FILE_DENY])
def test_onjs_por_tool_de_archivos_deny(tool, tool_input):
    rc, decision, reason = decide(tool, tool_input)
    assert (rc, decision) == (0, "deny")
    assert "AUTO-GENERADO" in reason and "on_src" in reason and "build_on_static" in reason


ONJS_FILE_OK = [
    ("Edit", {"file_path": "apps/web/on_src/unified.js", "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": "apps/web/static/js/fci.js", "content": "x"}),
    ("Write", {"file_path": "apps/web/static/js/on.js.map", "content": "x"}),
    ("Write", {"file_path": "apps/web/static/js/other-on.js", "content": "x"}),
    ("Read", {"file_path": ONJS}),  # leerlo está bien; y Read es una tool sin reglas
]


@pytest.mark.parametrize("tool,tool_input", ONJS_FILE_OK, ids=[f"{t}:{i['file_path']}" for t, i in ONJS_FILE_OK])
def test_archivos_que_no_son_onjs_sin_opinion(tool, tool_input):
    assert decide(tool, tool_input) == (0, None, "")


ONJS_SHELL_DENY = [
    ("ps", r"Set-Content apps\web\static\js\on.js -Value 'x'"),
    ("ps", r"Set-Content -Path apps\web\static\js\on.js -Value 'x'"),
    ("ps", "'x' | Out-File apps/web/static/js/on.js -Encoding utf8"),
    ("ps", r"Add-Content apps\web\static\js\on.js 'x'"),
    ("ps", r"Copy-Item build\on.js apps\web\static\js\on.js"),
    ("ps", r"Move-Item build\on.js -Destination apps\web\static\js\on.js"),
    ("ps", r"echo x > apps\web\static\js\on.js"),
    ("sh", "echo x > apps/web/static/js/on.js"),
    ("sh", "echo x >> apps/web/static/js/on.js"),
    ("sh", "echo x >apps/web/static/js/on.js"),
    ("sh", "echo x 1> apps/web/static/js/on.js"),
    ("sh", "cat build/on.js | tee apps/web/static/js/on.js"),
    ("sh", "cat build/on.js | tee -a apps/web/static/js/on.js > /dev/null"),
    ("sh", "sed -i 's/a/b/' apps/web/static/js/on.js"),
    ("sh", "sed -i.bak -e 's/a/b/' apps/web/static/js/on.js"),
    ("sh", "sed --in-place 's/a/b/' apps/web/static/js/on.js"),
    ("sh", "cp build/on.js apps/web/static/js/on.js"),
    ("sh", "mv build/on.js apps/web/static/js/on.js"),
    ("sh", 'echo x > "apps/web/static/js/on.js"'),
    ("sh", "cd apps/web/static/js && echo x > on.js"),
]


@pytest.mark.parametrize("tool,cmd", ONJS_SHELL_DENY, ids=[f"{t}:{c}" for t, c in ONJS_SHELL_DENY])
def test_onjs_por_shell_deny(tool, cmd):
    rc, decision, reason = (ps if tool == "ps" else sh)(cmd)
    assert (rc, decision) == (0, "deny")
    assert "build_on_static" in reason


ONJS_SHELL_OK = [
    ("sh", "py -3.12 scripts/build_on_static.py"),  # la regeneración NO es una escritura por shell
    ("ps", "py -3.12 scripts/build_on_static.py"),
    ("sh", "cat apps/web/static/js/on.js"),
    ("sh", "git diff apps/web/static/js/on.js"),
    ("sh", "cp apps/web/static/js/on.js /tmp/backup.js"),  # on.js es ORIGEN, no destino
    ("ps", r"Copy-Item apps\web\static\js\on.js C:\tmp\backup.js"),
    ("ps", r"Get-Content apps\web\static\js\on.js"),
    ("sh", "sed -n '1,5p' apps/web/static/js/on.js"),  # sed sin -i solo lee
    ("sh", "echo x > apps/web/static/js/fci.js"),
    ("sh", "echo x > apps/web/on_src/unified.js"),
]


@pytest.mark.parametrize("tool,cmd", ONJS_SHELL_OK, ids=[f"{t}:{c}" for t, c in ONJS_SHELL_OK])
def test_onjs_lecturas_y_regeneracion_sin_opinion(tool, cmd):
    assert (ps if tool == "ps" else sh)(cmd) == (0, None, "")


# --------------------------------------------------------------------------- #
# 2. Secretos: .env y jwt_secret no se leen desde el agente
# --------------------------------------------------------------------------- #
SECRET_DENY = [
    ("ps", "Get-Content .env"),
    ("ps", "gc .env"),
    ("ps", "Get-Content -Path .env"),
    ("ps", "Select-String BYMADATA .env"),
    ("ps", "Get-Item .env | Get-Content"),
    ("ps", r"type C:\Users\x\AppData\Local\monitor\jwt_secret"),
    ("ps", r"Get-Content $env:LOCALAPPDATA\monitor\jwt_secret"),
    ("sh", "cat .env"),
    ("sh", "cat ./.env"),
    ("sh", "head -n 5 .env"),
    ("sh", "tail .env"),
    ("sh", "less .env"),
    ("sh", "grep BYMADATA .env"),
    ("sh", "sed -n '1p' .env"),
    ("sh", "cat .env.local"),
    ("sh", "cat .env.production"),
    ("sh", "cat /var/lib/monitor/jwt_secret"),
    ("sh", "ssh monitor-do cat /var/lib/monitor/jwt_secret"),
    ("sh", "cat ~/.local/share/monitor/jwt_secret"),
    ("sh", "echo $(cat .env)"),
    ("sh", "cat `pwd`/.env"),
    ("sh", 'py -3.12 -c "print(open(\'.env\').read())"'),
]


@pytest.mark.parametrize("tool,cmd", SECRET_DENY, ids=[f"{t}:{c}" for t, c in SECRET_DENY])
def test_secretos_deny(tool, cmd):
    rc, decision, reason = (ps if tool == "ps" else sh)(cmd)
    assert (rc, decision) == (0, "deny")
    assert "agents.md" in reason and "config.settings" in reason


SECRET_OK = [
    ("sh", "cat .env.example"),
    ("ps", "Get-Content .env.example"),
    ("sh", "ls -la .env"),
    ("sh", "git check-ignore .env"),
    ("sh", "git status"),
    ("ps", "Test-Path .env"),
    ("ps", "dir .env"),
    ("sh", "cat config/settings.py"),
    ("sh", "cat .venv/pyvenv.cfg"),  # .venv no es .env
    ("sh", "grep -rn env_file config/"),
    ("sh", "py -3.12 -c \"from config.settings import settings; print(settings.market_source)\""),
]


@pytest.mark.parametrize("tool,cmd", SECRET_OK, ids=[f"{t}:{c}" for t, c in SECRET_OK])
def test_secretos_falsos_positivos_sin_opinion(tool, cmd):
    assert (ps if tool == "ps" else sh)(cmd) == (0, None, "")


# --------------------------------------------------------------------------- #
# 3. Bypass de TLS
# --------------------------------------------------------------------------- #
TLS_DENY = [
    ("sh", "curl -k https://x"),
    ("sh", "curl --insecure https://x"),
    ("sh", "curl -sSk https://x -o out.json"),
    ("sh", "curl -Lk https://x"),
    ("sh", "wget --no-check-certificate https://x"),
    ("ps", "Invoke-WebRequest -Uri x -SkipCertificateCheck"),
    ("ps", "Invoke-RestMethod -Uri x -SkipCertificateCheck:$true"),
    ("ps", "curl -k https://x"),
    ("ps", "iwr https://x -skipcertificatecheck"),
]


@pytest.mark.parametrize("tool,cmd", TLS_DENY, ids=[f"{t}:{c}" for t, c in TLS_DENY])
def test_tls_bypass_deny(tool, cmd):
    rc, decision, reason = (ps if tool == "ps" else sh)(cmd)
    assert (rc, decision) == (0, "deny")
    assert "MONITOR_TLS_NO_VERIFY_HOSTS" in reason


TLS_OK = [
    ("sh", "curl https://x"),
    ("sh", "curl -sS -o out.json https://x"),
    ("sh", "curl -K curlrc https://x"),  # -K (mayúscula) es --config, no --insecure
    ("sh", "wget https://x"),
    ("ps", "Invoke-WebRequest -Uri https://x"),
]


@pytest.mark.parametrize("tool,cmd", TLS_OK, ids=[f"{t}:{c}" for t, c in TLS_OK])
def test_tls_normal_sin_opinion(tool, cmd):
    assert (ps if tool == "ps" else sh)(cmd) == (0, None, "")


# --------------------------------------------------------------------------- #
# 4. Force push
# --------------------------------------------------------------------------- #
FORCE_PUSH = [
    "git push --force origin main",
    "git push -f",
    "git push -f origin main",
    "git push origin main -f",
    "git push origin main --force",
    "git push origin +main",
    "git push origin +HEAD:main",
    "git push --force-with-lease",
    "git push --force-with-lease=main origin main",
    "git push --force-if-includes origin main",
    "git push -fu origin main",
    "git -C /srv/app push -f origin main",
    "git -c push.default=current push --force",
]


@pytest.mark.parametrize("cmd", FORCE_PUSH)
@pytest.mark.parametrize("tool", ["sh", "ps"])
def test_force_push_deny(tool, cmd):
    rc, decision, reason = (ps if tool == "ps" else sh)(cmd)
    assert (rc, decision) == (0, "deny")
    assert "0.1.6" in reason


PUSH_OK = [
    "git push origin main",
    "git push",
    "git push -u origin fase-2-permisos",
    "git push origin HEAD:main",
    "git push origin fase-2-permisos:fase-2-permisos",
    "git push --no-force-with-lease origin main",
    "git push --dry-run origin main",
    "git fetch --force origin",  # el flag es de fetch, no de push
]


@pytest.mark.parametrize("cmd", PUSH_OK)
def test_push_normal_sin_opinion(cmd):
    assert sh(cmd) == (0, None, "")


# --------------------------------------------------------------------------- #
# 5. Destructivos de git → ask
# --------------------------------------------------------------------------- #
GIT_ASK = [
    "git checkout -- archivo.py",
    "git checkout -- .",
    "git checkout .",
    "git checkout main -- apps/web/app.py",
    "git checkout HEAD~1 apps/web/app.py",
    "git checkout -f main",
    "git restore archivo.py",
    "git restore --staged archivo.py",
    "git restore .",
    "git reset --hard HEAD~1",
    "git reset --hard",
    "git reset --merge",
    "git clean -fd",
    "git clean -n",
    "git stash drop",
    "git stash drop stash@{1}",
    "git stash clear",
    "git branch -D feature",
    "git branch --delete --force feature",
    "git branch -fd feature",
]


@pytest.mark.parametrize("cmd", GIT_ASK)
@pytest.mark.parametrize("tool", ["sh", "ps"])
def test_git_destructivo_ask(tool, cmd):
    rc, decision, reason = (ps if tool == "ps" else sh)(cmd)
    assert (rc, decision) == (0, "ask")
    assert reason


GIT_OK = [
    "git checkout -b feature",
    "git checkout -B feature",
    "git checkout -b feature main",
    "git checkout main",
    "git checkout fase-2-permisos",
    "git checkout -q main",
    "git checkout --track origin/main",
    "git checkout --orphan nueva",
    "git reset HEAD~1",
    "git reset --soft HEAD~1",
    "git reset apps/web/app.py",
    "git stash",
    "git stash pop",
    "git stash list",
    'git stash push -m "drop cache"',
    "git branch -d feature",
    "git branch feature",
    "git branch -a",
    "git status",
    "git log --oneline -5",
    "git diff -- apps/web/app.py",
    "git commit -m 'checkout -- x'",  # el texto va entre comillas: no es argv de checkout
]


@pytest.mark.parametrize("cmd", GIT_OK)
def test_git_normal_sin_opinion(cmd):
    assert sh(cmd) == (0, None, "")


# --------------------------------------------------------------------------- #
# 6. Intérprete/instalador pelado → ask (la convención es `py -3.12`)
# --------------------------------------------------------------------------- #
INTERP_ASK = [
    ("sh", "python script.py"),
    ("sh", "python3 -m pytest tests -q"),
    ("sh", "pip install x"),
    ("sh", "pip3 install -r requirements.txt"),
    ("sh", "python -m pip install x"),
    ("ps", "python script.py"),
    ("ps", "python.exe script.py"),
    ("ps", "pip install x"),
    ("ps", "& python script.py"),
    ("sh", "MONITOR_AS_OF=2026-01-01 python script.py"),
]


@pytest.mark.parametrize("tool,cmd", INTERP_ASK, ids=[f"{t}:{c}" for t, c in INTERP_ASK])
def test_interprete_pelado_ask(tool, cmd):
    rc, decision, reason = (ps if tool == "ps" else sh)(cmd)
    assert (rc, decision) == (0, "ask")
    assert "py -3.12" in reason


INTERP_OK = [
    ("sh", "py -3.12 -m pytest tests -q"),
    ("sh", "py -3.12 -m pytest tests/test_claude_guard.py -q -p no:cacheprovider"),
    ("sh", "py -3.12 run.py"),
    ("sh", "py -3.12 -m ruff check ."),
    ("sh", "py -3.12 -m pip install -r requirements.lock"),
    ("ps", "py -3.12 scripts/ingest_master.py"),
    ("ps", r'& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" run.py'),
    ("ps", "$py = \"$env:LOCALAPPDATA\\Programs\\Python\\Python312\\python.exe\""),
    ("sh", "pwsh scripts/check.ps1"),
    ("sh", "echo python"),
    ("sh", "which python"),
    ("sh", "ls pipelines/"),
]


@pytest.mark.parametrize("tool,cmd", INTERP_OK, ids=[f"{t}:{c}" for t, c in INTERP_OK])
def test_interprete_convencion_sin_opinion(tool, cmd):
    assert (ps if tool == "ps" else sh)(cmd) == (0, None, "")


# --------------------------------------------------------------------------- #
# 7. Borrado recursivo → ask
# --------------------------------------------------------------------------- #
RM_ASK = [
    ("sh", "rm -rf build"),
    ("sh", "rm -r build"),
    ("sh", "rm -fr build"),
    ("sh", "rm -Rf build"),
    ("sh", "rm --recursive build"),
    ("ps", "Remove-Item -Recurse -Force build"),
    ("ps", "Remove-Item build -Recurse"),
    ("ps", "remove-item build -recurse"),
    ("ps", "ri -Recurse build"),
    ("ps", "rm -r build"),
    ("ps", "rm -Recurse build"),
    ("ps", "rmdir /s /q build"),
    ("ps", "rd /s /q build"),
    ("ps", "cmd /c rmdir /s /q build"),
]


@pytest.mark.parametrize("tool,cmd", RM_ASK, ids=[f"{t}:{c}" for t, c in RM_ASK])
def test_borrado_recursivo_ask(tool, cmd):
    rc, decision, reason = (ps if tool == "ps" else sh)(cmd)
    assert (rc, decision) == (0, "ask")
    assert reason


RM_OK = [
    ("sh", "rm file.txt"),
    ("sh", "rm -f file.txt"),
    ("sh", "rm -v file.txt"),
    ("ps", "Remove-Item file.txt"),
    ("ps", "Remove-Item -Force file.txt"),
    ("ps", "Remove-Item -Path file.txt -Force"),
    ("sh", "rmdir build"),
    ("sh", "git rm --cached file.txt"),
]


@pytest.mark.parametrize("tool,cmd", RM_OK, ids=[f"{t}:{c}" for t, c in RM_OK])
def test_borrado_simple_sin_opinion(tool, cmd):
    assert (ps if tool == "ps" else sh)(cmd) == (0, None, "")


# --------------------------------------------------------------------------- #
# Comandos compuestos: basta que UN subcomando gatille; deny gana sobre ask
# --------------------------------------------------------------------------- #
COMPOUND = [
    ("sh", "git status && git push -f origin main", "deny"),
    ("sh", "git status; git push --force", "deny"),
    ("sh", "git status || git push -f", "deny"),
    ("sh", "ls; rm -rf build", "ask"),
    ("sh", "ls && python script.py", "ask"),
    ("sh", "rm -rf build; git push -f origin main", "deny"),
    ("sh", "git push -f origin main; rm -rf build", "deny"),
    ("sh", "cat README.md | grep x", None),
    ("sh", "git add . && git commit -m 'x' && git push origin main", None),
    ("sh", "git status\ngit push -f origin main", "deny"),
    ("sh", "git status\r\ngit reset --hard", "ask"),
    ("ps", "git status; git push -f origin main", "deny"),
    ("ps", "Get-ChildItem | Select-Object Name; Get-Content .env", "deny"),
    ("ps", "git status; Remove-Item -Recurse build", "ask"),
    ("ps", "if (Test-Path .env) { Get-Content .env }", "deny"),
    ("ps", "if (Test-Path .env) { 'existe' }", None),
    ("sh", 'git commit -m "no toca .env ni rm -rf; && git push -f"', None),
]


@pytest.mark.parametrize("tool,cmd,expected", COMPOUND, ids=[f"{t}:{c!r}" for t, c, _ in COMPOUND])
def test_compuestos(tool, cmd, expected):
    rc, decision, _ = (ps if tool == "ps" else sh)(cmd)
    assert (rc, decision) == (0, expected)


# --------------------------------------------------------------------------- #
# Contrato del hook: stdin inválido → exit 1 sin stdout; tool desconocida → sin opinión
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stdin_text", ["", "   ", "{not json", "[1, 2]", "null", '"str"'], ids=repr)
def test_stdin_invalido_exit_1_sin_stdout(stdin_text):
    proc = _run(stdin_text)
    assert proc.returncode == 1  # NO 2 (bloquearía) ni 0 (pasaría como "sin opinión")
    assert proc.stdout == ""
    assert proc.stderr.strip()
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize(
    "tool,tool_input",
    [
        ("Read", {"file_path": ".env"}),  # Read lo cubre el permiso Read(./.env); acá no hay regla
        ("Glob", {"pattern": "**/*.py"}),
        ("Grep", {"pattern": "rm -rf"}),
        ("WebFetch", {"url": "https://x", "prompt": "git push -f"}),
        ("Bash", {}),  # sin command: nada que juzgar
        ("Bash", {"command": ""}),
        ("PowerShell", {"command": "   "}),
    ],
)
def test_tool_desconocida_o_sin_comando_sin_opinion(tool, tool_input):
    assert decide(tool, tool_input) == (0, None, "")


def test_payload_sin_tool_name_sin_opinion():
    proc = _run("{}")
    assert (proc.returncode, proc.stdout) == (0, "")


def test_json_de_salida_forma_exacta():
    proc = _run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push -f"}}))
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert set(out) == {"hookSpecificOutput"}
    assert set(out["hookSpecificOutput"]) == {"hookEventName", "permissionDecision", "permissionDecisionReason"}
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert proc.stdout.isascii()
