# ERE — Claude Operating Instructions

This file governs how Claude operates in this repository.
It overrides defaults where noted. For domain and architecture details, read the docs — not this file.

---

## Project at a glance

| Item | Reference |
|---|---|
| What ERE is | [README.md](README.md) |
| Architecture layers and patterns | [docs/architecture/ERE-OVERVIEW.md](docs/architecture/ERE-OVERVIEW.md) |
| ERS–ERE interface contract | [docs/ERS-ERE-System-Technical-Contract.pdf](docs/ERS-ERE-System-Technical-Contract.pdf) |
| Cosmic Python blueprint | [docs/architecture/ERE-COSMIC-PYTHON-ARCHITECTURE.md](docs/architecture/ERE-COSMIC-PYTHON-ARCHITECTURE.md) |
| Current task | [WORKING.md](WORKING.md) → `docs/tasks/yyyy-mm-dd-*.md` |

---

## Before you start — always

1. Read `WORKING.md` — it points to the current active task.
2. Read the referenced `docs/tasks/yyyy-mm-dd-*.md` fully (spec + history).
3. If the task file is missing, create it before doing anything else.
4. Align changes with `README.md` intent and `docs/architecture/` decisions.

> The task file is a **living document** — update it as you progress, not only at the end.
> Record what you changed, why, what you learned, and what comes next.

---

## Skills

Before planning or coding, load these skills from `/.claude/skills/`:

| Skill | When to apply |
|---|---|
| `stream-coding` | Primary workflow driver for all implementation |
| `cosmic-python` | Layer design, ports/adapters, SOLID enforcement |
| `bdd` | Scenario writing, step definitions, feature files |
| `gitnexus` | Codebase navigation, blast-radius analysis |
| `git-commit-and-pr` | Commit hygiene, PR narrative |

If a skill conflicts with the task file, the **task file wins** for that scope.
If a skill conflicts with architecture constraints, **stop and surface the conflict**.

---

## Repository structure

```
src/ere/
├── adapters/        # Infrastructure: Redis, cluster store, resolver strategies
├── entrypoints/     # Thin drivers: Redis pub/sub consumer
├── models/          # Domain models from erspec + ERE extensions (no I/O)
└── services/        # Use-case orchestration

test/
├── features/        # Gherkin BDD feature files
├── steps/           # pytest-bdd step definitions
└── test_data/       # RDF fixtures (Turtle)

docs/
├── tasks/           # yyyy-mm-dd-<task>.md — spec + engineering diary
└── architecture/    # ERE-OVERVIEW.md, sequence diagrams, ADRs
```

---

## How to work — stream loop

Repeat until the task-file acceptance criteria are all checked:

1. **Orient** — re-read WORKING.md and the task file; identify the next smallest slice.
2. **Slice** — define one vertical increment; state it in one sentence of domain language.
3. **Prove** — write a failing test (unit) or scenario (BDD) first.
4. **Implement** — minimal code to pass; stay within layer boundaries.
5. **Refactor** — remove duplication, clarify naming, keep domain pure.
6. **Record** — update the task file: slice / change / result / notes / next.
7. **Commit** — small, coherent increment aligned with the slice. Never add coauthors or tool names in commit messages (e.g. claude, haiku, etc.).

**If you cannot describe the slice in one sentence, it is too large.**

---

## Architecture rules

Dependency direction is enforced at CI time via `importlinter`. Never violate it:

```
entrypoints → services → models
                       ↘
                       adapters → models
```

- `models/` — no framework imports, no I/O, no side effects.
- `adapters/` — infrastructure only; never call `services/`.
- `services/` — orchestrate domain and adapters; never import from `entrypoints/`.
- `entrypoints/` — parse input, call services, format output; no business logic.

- use best practices for OpenTelemetry and logging, but do not instrument prematurely — wait until a clear need arises to understand execution flows or debug issues.


Anti-patterns to refuse:
- I/O or framework imports inside `models/`
- Business rules inside `adapters/` or `entrypoints/`
- Magic strings or raw dicts where constants/enums belong
- Circular imports between layers or modules
- imports are not groupped in teh header of the module but are scattered across the code

---

## Testing rules

- **Unit tests per layer** — each layer tests its own responsibility only.
- **BDD for service use cases** — feature files in `test/features/`, steps in `test/steps/`.
- **TDD by default** — write the failing test before the implementation.
- **80 %+ coverage** target on new production code.
- Use `pytest-bdd` with `target_fixture` (no `ctx` dict); use `parsers.re` when `parsers.parse` cannot handle edge cases (empty strings, quoted numeric values).

---

## Commit rules

Format: `type(scope): concise description`

Examples:
- `test(services): add BDD scenario for conflict detection`
- `feat(adapters): implement content-hash mock resolver`
- `refactor(entrypoints): extract request validation to services`

**Never include** co-author lines, tool names, agent names, or internal implementation details in commit messages. Focus on the *what* and *why* of the code change.

## Git hygiene rules

- **Never stage or commit files you haven't modified** — use `git add <file>` to stage only your changes, or `git add -p` to review hunks before staging.
- **Never revert or reset files outside your current task scope**.
---

## Autonomy rules

You **may** do without asking:
- Refactor for clarity within the current task scope
- Strengthen tests and BDD scenarios
- Extract ports/adapters when coupling appears
- Update the task file and README

You **must not** do without explicit instruction:
- Expand scope beyond WORKING.md
- Introduce speculative features
- Break architecture boundaries
- Make domain modelling decisions without evidence — ask first

---

## Definition of done

A task slice is done when:
- [ ] Tests pass and coverage is meaningful for the new behaviour
- [ ] Layer boundaries are clean (no I/O in models, no business logic in entrypoints)
- [ ] Task file is updated (slice / change / result / notes / next)
- [ ] No silent TODOs — follow-ups are explicitly recorded
- [ ] Any non-trivial architectural decision is captured as an ADR-lite note

---

<!-- gitnexus:start -->
# GitNexus MCP

This project is indexed by GitNexus as **ere-basic** (344 symbols, 700 relationships, 16 execution flows).

GitNexus provides a knowledge graph over this codebase — call chains, blast radius, execution flows, and semantic search.

## Always Start Here

For any task involving code understanding, debugging, impact analysis, or refactoring, you must:

1. **Read `gitnexus://repo/{name}/context`** — codebase overview + check index freshness
2. **Match your task to a skill below** and **read that skill file**
3. **Follow the skill's workflow and checklist**

> If step 1 warns the index is stale, run `npx gitnexus analyze` in the terminal first.

## Skills

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |

## Tools Reference

| Tool | What it gives you |
|------|-------------------|
| `query` | Process-grouped code intelligence — execution flows related to a concept |
| `context` | 360-degree symbol view — categorized refs, processes it participates in |
| `impact` | Symbol blast radius — what breaks at depth 1/2/3 with confidence |
| `detect_changes` | Git-diff impact — what do your current changes affect |
| `rename` | Multi-file coordinated rename with confidence-tagged edits |
| `cypher` | Raw graph queries (read `gitnexus://repo/{name}/schema` first) |
| `list_repos` | Discover indexed repos |

## Resources Reference

Lightweight reads (~100-500 tokens) for navigation:

| Resource | Content |
|----------|---------|
| `gitnexus://repo/{name}/context` | Stats, staleness check |
| `gitnexus://repo/{name}/clusters` | All functional areas with cohesion scores |
| `gitnexus://repo/{name}/cluster/{clusterName}` | Area members |
| `gitnexus://repo/{name}/processes` | All execution flows |
| `gitnexus://repo/{name}/process/{processName}` | Step-by-step trace |
| `gitnexus://repo/{name}/schema` | Graph schema for Cypher |

## Graph Schema

**Nodes:** File, Function, Class, Interface, Method, Community, Process
**Edges (via CodeRelation.type):** CALLS, IMPORTS, EXTENDS, IMPLEMENTS, DEFINES, MEMBER_OF, STEP_IN_PROCESS

```cypher
MATCH (caller)-[:CodeRelation {type: 'CALLS'}]->(f:Function {name: "myFunc"})
RETURN caller.name, caller.filePath
```

<!-- gitnexus:end -->
