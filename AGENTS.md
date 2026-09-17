# ERE — Agent Operating Instructions

This file governs how AI agents operate in this repository.
It is the canonical agent instruction file; the root `CLAUDE.md` only imports it. See also `.claude/CLAUDE.md` (project instructions).

---

## Commits and PRs

- **Never auto-commit** unless the user explicitly asks.
- **Never force-push** to `main` or `develop`.
- **Never add co-author lines**, tool names, or agent names to commit messages.
- Commit format: `type(scope): concise description` — e.g. `feat(adapters): add splink resolver factory`.
- Stage only files you modified: `git add <file>`, never `git add -A` blindly.
- Before committing, run `make lint` and `make test-unit` to verify nothing is broken.
- PRs target `develop` (not `main`) unless told otherwise.
- When creating a PR, include a short summary and a test-plan checklist.

---

## Working Methodology

### Before touching code

1. Read `WORKING.md` — it points to the active task file.
2. Read the referenced `docs/tasks/yyyy-mm-dd-*.md` fully.
3. Understand the current branch state: `git log --oneline -10`.

### Running the stack for integration tests

Integration tests require Redis to be running. Start it first:

```bash
make infra-up          # starts Redis + RedisInsight via Docker Compose
make test-integration  # then run integration tests
make infra-down        # tear down when done
```

Unit tests do **not** require any infrastructure:

```bash
make test-unit         # fast, self-contained, uses your venv
```

### Typical development loop

```bash
make install           # first time or after pyproject.toml changes
make test-unit         # red → green → refactor
make lint              # quick style check
make check-architecture  # verify import-linter contracts
make all-quality-checks  # before opening a PR
```

---

## Tooling Reference

| Target | What it does |
|--------|-------------|
| `make install` | Install deps via Poetry |
| `make test-unit` | pytest unit suite + coverage report |
| `make test-integration` | integration tests (Redis must be up) |
| `make test-coverage` | HTML coverage report → `htmlcov/index.html` |
| `make lint` | pylint (fast, your venv) |
| `make format` | Ruff formatter |
| `make lint-fix` | Ruff auto-fix |
| `make check-clean-code` | pylint + radon + xenon (tox isolated) |
| `make check-architecture` | import-linter contracts (tox isolated) |
| `make all-quality-checks` | lint + clean-code + architecture |
| `make ci` | full tox pipeline (py312 + architecture + clean-code) |
| `make infra-up` | Start Redis stack (Docker Compose) |
| `make infra-down` | Stop Redis stack |
| `make infra-watch` | Live-reload mode (syncs `src/` and `src/config/`) |

---

## Architecture Rules (enforced by import-linter)

Dependency direction must never be violated:

```
entrypoints → services → models
                       ↘
                       adapters → models
```

- `models/` — no I/O, no framework imports, no side effects.
- `adapters/` — infrastructure only; never calls `services/`.
- `services/` — orchestrates domain and adapters; never imports from `entrypoints/`.
- `entrypoints/` — parses input, calls services, formats output; no business logic.

Violations block CI. Check with `make check-architecture` before opening a PR.

---

## Memory Conventions

Save to memory only what is non-obvious and persists across conversations:

- Architectural decisions that aren't evident from the code (e.g. resolver factory registry pattern, DuckDB threading model).
- Design constraints explained by the user that aren't in comments or docs.
- User preferences about how to collaborate (e.g. "never suggest walrus operators", "prefer explicit factory injection").

Do **not** save to memory:
- Current task state (use the task file in `docs/tasks/`).
- Git history or recent changes (readable via `git log`).
- File paths or code structure (readable from the repo).

---

## Spec-Driven Workflow (OpenSpec) — Golden Thread

Work is shaped and tracked in `openspec/` using the pinned `meaningfy` schema (`openspec/schemas/meaningfy/`, OpenSpec 1.4.1).

- **EPIC** ≡ `openspec/changes/<id>/proposal.md` (Shape-Up work shape: appetite + no-gos).
- **PLAN** ≡ `design.md` + `tasks.md` — scored by the clarity gate (≥9/10) before `/opsx:apply`.
- **Normative requirements** ≡ `specs/` deltas (RFC-2119 SHALL + Given/When/Then scenarios).
- **Truth** ≡ `openspec/specs/` — deltas merge there on `/opsx:archive`; if anything else disagrees, specs win.

**Golden thread — cite your parent:** EPIC → PLAN → specs → commit.
- `tasks.md` cites its parent EPIC (change id) on the first line.
- Each spec delta names its capability.
- Commits reference the change id they implement.

Commands: `/opsx:explore`, `/opsx:propose`, `/opsx:apply`, `/opsx:sync`, `/opsx:archive`.
Validate with `make check-specs` (`openspec validate --all --strict`).

---

## Gotchas

- **`logging.basicConfig` is a no-op** when handlers already exist (conftest sets them up via `dictConfig`). Mock it with `patch("logging.basicConfig")` in logging tests.
- **DuckDB in tests**: use in-memory mode (`:memory:`) or a temp file via `tmp_path`; never a fixed path that leaks between tests.
- **Integration tests are marked** with `@pytest.mark.integration` — `make test-unit` skips them automatically.
- **`infra/.env`** is required for `make infra-*` targets. Copy from `infra/.env.example` on first use.
- **Config files** live in `src/config/` (moved from repo root in the 2026-04 restructure). Do not confuse with `infra/config/`.
- **erspec models** are LinkML-generated with snake_case fields (e.g. `legal_name`, not `legalName`). Do not edit generated files — update the schema and regenerate.
- **`ERE_LOG_LEVEL`** is the canonical env var for log level in this service (not `LOG_LEVEL`).

---

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **entity-resolution-engine-basic** (846 symbols, 1313 relationships, 17 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "develop"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "develop" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/entity-resolution-engine-basic/context` | Codebase overview, check index freshness |
| `gitnexus://repo/entity-resolution-engine-basic/clusters` | All functional areas |
| `gitnexus://repo/entity-resolution-engine-basic/processes` | All execution flows |
| `gitnexus://repo/entity-resolution-engine-basic/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
