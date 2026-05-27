# docs/superpowers/ — Flujo de trabajo con Superpowers

Este directorio guarda los artefactos del método **Superpowers** (plugin de Claude Code)
aplicado al Monitor. El flujo completo es:

```
brainstorming → spec → writing-plans → plan → TDD/subagent-driven → code-review → finishing-branch
```

## Carpetas

| Carpeta | Qué guarda | Skill que lo produce |
|---------|------------|----------------------|
| `specs/` | Diseños aprobados antes de implementar | `brainstorming` |
| `plans/` | Planes de implementación paso a paso (TDD, commits) | `writing-plans` |

## Convención de nombres

- **Spec:** `specs/YYYY-MM-DD-<topic>-design.md`
- **Plan:** `plans/YYYY-MM-DD-<feature-name>.md`

Un spec/plan por **subsistema independiente** (regla YAGNI + scope-check de Superpowers).
Si una idea abarca varios subsistemas, se decompone: cada uno tiene su ciclo
spec → plan → implementación.

## Specs retroactivos

Los specs con `> Retroactivo` en el encabezado documentan features que se construyeron
**antes** de adoptar el método (el WIP de mayo 2026). No siguieron el flujo
brainstorm-first; se escribieron mirando el código ya hecho para dejar el registro
de diseño que faltaba. Las features nuevas **sí** arrancan por `brainstorming`.

Ver la sección "Flujo Superpowers" en [`CLAUDE.md`](../../CLAUDE.md) para cuándo aplica
cada skill y las particularidades de este proyecto (OneDrive, worktrees, intérprete).
