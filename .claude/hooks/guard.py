"""Hook PreToolUse de Claude Code: la SEGUNDA capa de permisos del repo.

La primera capa son los `permissions.allow/ask/deny` de `.claude/settings.json`, que ya
cubren `Bash(...)`. Este hook existe porque hay cosas que un patrón de permisos no expresa
(agents.md §0.5: "el `if` de los hooks es best-effort; lo duro va en permisos; este hook
cubre PowerShell, escrituras por shell y argv"): la tool **PowerShell** (una regla
`Bash(...)` no la cubre), las **escrituras por shell** (`>`, `tee`, `sed -i`, `Set-Content`
sobre `on.js`) y el **argv** de git (`git push -f`, refspec `+main`, `git checkout -- x`).

Contrato (doc oficial: https://code.claude.com/docs/en/hooks). Se invoca en forma exec
(`py -3.12 guard.py`), recibe por stdin el JSON del evento — `tool_name` y `tool_input`
(`command` para Bash y PowerShell; `file_path` para Edit/Write/MultiEdit,
`notebook_path` para NotebookEdit) — y responde:

  - BLOQUEAR:  stdout `{"hookSpecificOutput": {"hookEventName": "PreToolUse",
               "permissionDecision": "deny", "permissionDecisionReason": "..."}}` + exit 0
  - PREGUNTAR: idem con `"ask"` (muestra el prompt de permisos normal)
  - SIN OPINIÓN: exit 0 sin salida (sigue el flujo normal de permisos; NO aprueba)
  - error propio (stdin inválido, excepción): mensaje breve a stderr + exit 1, que es NO
    bloqueante. Nunca exit 2 (bloquea siempre) ni un traceback a stdout (JSON inválido).

Reglas: cada subcomando (partido por `&&`, `||`, `;`, `|`, `|&`, `&`, `$(`, `(`, `)`,
backtick y salto de línea, respetando comillas) se evalúa por separado; basta que UNO
gatille; si hay deny y ask a la vez, gana deny.

Solo stdlib, no lee disco, <50 ms. El JSON sale con `ensure_ascii=True` porque la
consola de Windows es cp1252.
"""

import json
import re
import sys

DENY, ASK = "deny", "ask"

REASON_ONJS = (
    "on.js es AUTO-GENERADO: editá apps/web/on_src/ y regenerá con "
    "`py -3.12 scripts/build_on_static.py`"
)
REASON_SECRET = (
    "los secretos no se leen desde el agente (agents.md §0.1); usá "
    '`py -3.12 -c "from config.settings import settings; ..."` si necesitás un valor derivado'
)
REASON_TLS = (
    "TLS se verifica siempre (core/infrastructure/_tls.py); "
    "la perilla es MONITOR_TLS_NO_VERIFY_HOSTS"
)
REASON_FORCE_PUSH = "force push: prohibido salvo OK explícito (agents.md §0.1.6)"
REASON_GIT = "{} pisa cambios locales; confirmá antes"
REASON_INTERP = "usá `py -3.12` (CLAUDE.md), no `{}` pelado"
REASON_RM = "borrado recursivo (`{}`): confirmá el path antes"

SHELL_TOOLS = {"Bash": "bash", "PowerShell": "ps"}
FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


# --------------------------------------------------------------------------- #
# Tokenización tolerante (sin shlex: rompe con backticks y `$env:` de PowerShell)
# --------------------------------------------------------------------------- #
_OPS2 = ("&&", "||", "|&", "$(")
_OPS1 = ";|&(){}`\n\r"  # llaves: `if (...) { rm -rf x }` de PowerShell, `${VAR}`


def split_subcommands(cmd, shell):
    """Parte en subcomandos por los operadores de la shell, respetando comillas.

    Escape: `\\` en bash, backtick en PowerShell (que por eso NO separa ahí)."""
    esc = "\\" if shell == "bash" else "`"
    parts, buf, quote, i, n = [], [], None, 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            elif quote == '"' and ch == esc and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 1
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == esc and i + 1 < n:
            buf.append(ch)
            buf.append(cmd[i + 1])
            i += 1
        elif cmd[i:i + 2] in _OPS2:
            parts.append("".join(buf))
            buf = []
            i += 1
        elif ch in _OPS1:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


_TOKEN_RE = re.compile(r'''(?:"(?:`.|\\.|[^"\\`])*"|'[^']*'|[^\s"'])+''', re.S)


def _unquote(tok):
    if len(tok) >= 2 and tok[0] in "\"'" and tok[-1] == tok[0]:
        return tok[1:-1]
    return tok.replace('"', "").replace("'", "")


def tokenize(sub):
    return [_unquote(m.group(0)) for m in _TOKEN_RE.finditer(sub)]


_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
_PREFIXES = {"&", "sudo", "nohup", "time", "exec", "cmd", "/c", "/k"}


def head(toks):
    """(nombre-base en minúsculas, primer token crudo, args) saltando `&`, `sudo`, `FOO=x`…"""
    i = 0
    while i < len(toks) and (toks[i].lower() in _PREFIXES or _ENV_ASSIGN_RE.match(toks[i])):
        i += 1
    if i >= len(toks):
        return "", "", []
    raw = toks[i]
    base = raw.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base, raw, toks[i + 1:]


def _short_flags(tok, letter):
    """`-rf`, `-fu`: cluster de flags cortos (un solo guion) que contiene `letter`."""
    return bool(re.match(r"^-[a-zA-Z]+$", tok)) and letter in tok[1:]


# --------------------------------------------------------------------------- #
# 1. on.js es AUTO-GENERADO
# --------------------------------------------------------------------------- #
# `**/static/js/on.js` (relativo o absoluto, `/` o `\`) o el `on.js` pelado de un
# `cd apps/web/static/js && ...` (es el único on.js del repo). NO `build/on.js`.
_ONJS_RE = re.compile(r"^(?:.*/)?static/js/on\.js$|^(?:\./)?on\.js$")


def is_onjs(path):
    return bool(_ONJS_RE.match(path.replace("\\", "/").lower()))


_REDIR_RE = re.compile(r"^\d*>>?(.*)$")
_WRITE_ANY = {"tee", "set-content", "sc", "out-file", "add-content", "ac",
              "new-item", "ni", "rename-item", "ren", "rni"}
_WRITE_DST = {"cp", "mv", "copy", "move", "copy-item", "cpi", "move-item", "mi"}


def _destination(args):
    for i, a in enumerate(args):
        if a.lower().startswith("-dest") and i + 1 < len(args):
            return args[i + 1]
    positional = [a for a in args if not a.startswith("-")]
    return positional[-1] if positional else ""


def rule_onjs_shell(toks):
    for i, t in enumerate(toks):
        m = _REDIR_RE.match(t)
        if m:
            dst = m.group(1) or (toks[i + 1] if i + 1 < len(toks) else "")
            if is_onjs(dst):
                return True
    base, _, args = head(toks)
    if base in _WRITE_ANY and any(is_onjs(a) for a in args):
        return True
    if base in _WRITE_DST and is_onjs(_destination(args)):
        return True
    if base == "sed" and any(is_onjs(a) for a in args):  # sin -i, sed solo lee
        return any(a.startswith(("-i", "--in-place")) or _short_flags(a, "i") for a in args)
    return False


# --------------------------------------------------------------------------- #
# 2. Secretos: `.env` (salvo `.env.example`) y `monitor/jwt_secret`
# --------------------------------------------------------------------------- #
_ENV_RE = re.compile(r"(?<![\w.\-])\.env(?:\.[\w\-]+)*(?![\w\-])")
_JWT_RE = re.compile(r"monitor[/\\]jwt_secret(?![\w\-])", re.I)
_LIST_ONLY = {"git", "ls", "dir", "test-path", "get-childitem", "gci", "find", "stat", "tree"}


def rule_secret(text, base):
    if base in _LIST_ONLY:
        return False
    if any(m.group(0).lower() != ".env.example" for m in _ENV_RE.finditer(text)):
        return True
    return bool(_JWT_RE.search(text))


# --------------------------------------------------------------------------- #
# 3. Bypass de TLS
# --------------------------------------------------------------------------- #
def rule_tls(toks, base, args):
    if any(t.lower().startswith("-skipcertificatecheck") for t in toks):
        return True
    if base == "curl":
        return any(a == "--insecure" or _short_flags(a, "k") for a in args)
    if base == "wget":
        return "--no-check-certificate" in args
    return False


# --------------------------------------------------------------------------- #
# 4/5. git: force push (deny) y destructivos (ask)
# --------------------------------------------------------------------------- #
_GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                          "--exec-path", "--config-env"}


def git_argv(base, args):
    """→ (subcomando, args) saltando las opciones globales de git, o None si no es git."""
    if base != "git":
        return None
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_GLOBAL_WITH_VALUE else 1
    if i >= len(args):
        return None
    return args[i].lower(), args[i + 1:]


def is_force_push(sub, args):
    if sub != "push":
        return False
    return any(a.startswith("--force") or _short_flags(a, "f") or (a.startswith("+") and len(a) > 1)
               for a in args)


def git_destructive(sub, args):
    """Nombre de la operación destructiva, o None."""
    positional = [a for a in args if not a.startswith("-")]
    if sub == "checkout":
        if any(a in ("-b", "-B", "--orphan") or a.startswith("--orphan=") for a in args):
            return None
        if "--" in args or "." in positional or len(positional) >= 2 \
                or any(a in ("-f", "--force") for a in args):
            return "git checkout (restaura archivos)"
        return None
    if sub == "restore":
        return "git restore"
    if sub == "reset" and any(a in ("--hard", "--merge") for a in args):
        return "git reset --hard/--merge"
    if sub == "clean":
        return "git clean"
    if sub == "stash" and positional and positional[0] in ("drop", "clear"):
        return "git stash " + positional[0]
    if sub == "branch":
        if any(_short_flags(a, "D") for a in args):
            return "git branch -D"
        flags = set(args)
        if (flags & {"--delete", "-d"} or any(_short_flags(a, "d") for a in args)) \
                and (flags & {"--force", "-f"} or any(_short_flags(a, "f") for a in args)):
            return "git branch --delete --force"
    return None


# --------------------------------------------------------------------------- #
# 6. Intérprete/instalador pelado (la convención del repo es `py -3.12`)
# --------------------------------------------------------------------------- #
_INTERP_RE = re.compile(r"^(python|pip)(3(\.\d+)?)?(\.exe)?$", re.I)


def rule_interp(raw):
    if "/" in raw or "\\" in raw:  # ruta explícita (p. ej. el 3.12 de Programs): no es "pelado"
        return None
    return raw if _INTERP_RE.match(raw) else None


# --------------------------------------------------------------------------- #
# 7. Borrado recursivo
# --------------------------------------------------------------------------- #
_RM = {"rm", "rmdir", "rd", "del", "erase", "ri", "remove-item"}


def rule_rm(base, args):
    if base not in _RM:
        return None
    for a in args:
        low = a.lower()
        # `-r`/`-rf`/`-Rf`/`-rfv` (cluster corto: `-Force` tiene 5 letras y NO entra),
        # `-Recurse` y sus abreviaturas PowerShell, `--recursive`, `/s` de cmd.
        if low in ("--recursive", "/s") or low.startswith("-rec") \
                or (re.match(r"^-[a-z]{1,4}$", low) and "r" in low):
            return f"{base} {a}"
    return None


# --------------------------------------------------------------------------- #
# Evaluación
# --------------------------------------------------------------------------- #
def evaluate_shell(cmd, shell):
    """→ (decision, reason) o None. deny gana sobre ask."""
    ask = None
    for sub in split_subcommands(cmd, shell):
        toks = tokenize(sub)
        if not toks:
            continue
        base, raw, args = head(toks)
        git = git_argv(base, args)
        if rule_onjs_shell(toks):
            return DENY, REASON_ONJS
        if rule_secret(sub, base):
            return DENY, REASON_SECRET
        if rule_tls(toks, base, args):
            return DENY, REASON_TLS
        if git and is_force_push(*git):
            return DENY, REASON_FORCE_PUSH
        if ask:
            continue
        what = git and git_destructive(*git)
        if what:
            ask = (ASK, REASON_GIT.format(what))
            continue
        interp = rule_interp(raw)
        if interp:
            ask = (ASK, REASON_INTERP.format(interp))
            continue
        rm = rule_rm(base, args)
        if rm:
            ask = (ASK, REASON_RM.format(rm))
    return ask


def evaluate(tool_name, tool_input):
    if tool_name in SHELL_TOOLS:
        cmd = tool_input.get("command")
        if isinstance(cmd, str) and cmd.strip():
            return evaluate_shell(cmd, SHELL_TOOLS[tool_name])
    elif tool_name in FILE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if isinstance(path, str) and is_onjs(path):
            return DENY, REASON_ONJS
    return None


def main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    if not isinstance(data, dict):
        raise ValueError("el payload del hook no es un objeto JSON")
    tool_input = data.get("tool_input")
    verdict = evaluate(data.get("tool_name"), tool_input if isinstance(tool_input, dict) else {})
    if verdict:
        decision, reason = verdict
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # exit 1 = error NO bloqueante; jamás traceback a stdout
        sys.stderr.write(f"guard.py: {type(exc).__name__}: {exc}"[:300] + "\n")
        sys.exit(1)
