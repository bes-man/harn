"""Built-in best-practice skill library + task→domain gap detection.

harn accumulates project-specific standards from the human (via `ask_user`),
but a brand-new project starts with empty skills. When a task clearly belongs
to a domain (frontend, backend, testing, …) and no skill covers it, harn can
bootstrap a solid industry-baseline skill from this library, which the agent
then refines with project specifics. This removes the cold-start gap where a
frontend task gets built with zero UI/accessibility guidance.

Flow:
  1. `gaps(env_dir, task)` infers the task's domains and returns those with no
     existing (non-empty) skill.
  2. `install(env_dir, name)` writes the baseline SKILL.md for a domain.
  3. The agent reads it, then captures project deviations via `save_to_skill`
     / `ask_user(skill=…)`.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import skills as skills_mod


@dataclass(frozen=True)
class LibrarySkill:
    name: str
    description: str
    keywords: tuple[str, ...]   # substrings that map a task to this domain
    aliases: tuple[str, ...]    # existing skill names that already satisfy it
    body: str


def _s(name, description, keywords, aliases, body) -> LibrarySkill:
    return LibrarySkill(name, description, keywords, aliases, body.strip())


# --------------------------------------------------------------------------- #
# The catalog. Bodies are concise industry baselines — enough to guide a first
# implementation, meant to be refined with project specifics.
# --------------------------------------------------------------------------- #
LIBRARY: dict[str, LibrarySkill] = {
    "frontend": _s(
        "frontend",
        "Frontend/UI best practices: components, state, accessibility, responsive design. Load for any user-facing work.",
        ("frontend", "front-end", "ui", "ux", "component", "css", "styl",
         "react", "vue", "svelte", "page", "screen", "button", "form",
         "responsive", "layout", "design", "интерфейс", "фронт", "кнопк",
         "форм", "экран", "верстк", "стил"),
        ("ui", "frontend"),
        """
# frontend

Industry-baseline frontend conventions. Refine with this project's stack.

## Components
- One responsibility per component; lift shared state up, keep leaves dumb.
- Co-locate component, styles, and tests. Name by role, not by appearance.
- Props are the contract — keep them minimal and typed; avoid boolean soup.

## State
- Local state first; promote to shared/store only when ≥2 components need it.
- Derive, don't duplicate: compute from source of truth instead of copying.
- Keep side effects (fetch, subscriptions) out of render; clean them up.

## Accessibility (a11y) — non-negotiable
- Semantic HTML first (`button`, `nav`, `label`); ARIA only to fill gaps.
- Every interactive element is keyboard-reachable and has a visible focus ring.
- Inputs have associated `<label>`; images have `alt`; color is never the only signal.
- Contrast ≥ 4.5:1 for body text. Respect `prefers-reduced-motion`.

## Responsive & performance
- Mobile-first; test at 320px, 768px, 1280px. Touch targets ≥ 44px.
- Lazy-load below-the-fold and heavy assets; budget bundle size.
- Avoid layout shift: reserve space for images/embeds.

## Quality
- No business logic in JSX; extract to hooks/helpers.
- Handle loading / empty / error states explicitly for every async view.
""",
    ),
    "backend": _s(
        "backend",
        "Backend/service best practices: layering, validation, errors, idempotency. Load for server-side work.",
        ("backend", "back-end", "server", "service", "endpoint", "handler",
         "controller", "route", "middleware", "worker", "queue", "cron",
         "бэк", "сервер", "сервис", "обработчик", "очеред"),
        ("backend", "api"),
        """
# backend

Industry-baseline backend conventions. Refine with this project's stack.

## Layering
- Separate transport (HTTP) ← service (logic) ← data (storage). No SQL in handlers.
- Handlers stay thin: parse → validate → call service → map result to response.

## Validation & errors
- Validate every external input at the boundary; never trust the client.
- Fail fast with typed errors; map them to stable status codes + safe messages.
- Never leak stack traces, secrets, or internal IDs in error responses.

## Reliability
- Make mutating endpoints idempotent (idempotency keys / upserts) where possible.
- Set timeouts and retries with backoff on every outbound call.
- Use transactions for multi-step writes; keep them short.

## Observability
- Structured logs with a correlation/request id; log decisions, not noise.
- Emit metrics for latency, error rate, and saturation of hot paths.

## Concurrency & data
- Assume requests run in parallel: guard shared state, avoid read-modify-write races.
- Paginate list endpoints; never return unbounded result sets.
""",
    ),
    "api": _s(
        "api",
        "API design best practices: REST/contract, versioning, pagination, auth. Load when designing or changing APIs.",
        ("api", "rest", "graphql", "openapi", "swagger", "contract",
         "endpoint design", "schema", "версион", "контракт"),
        ("api", "backend"),
        """
# api

Industry-baseline API design. Refine with this project's conventions.

## Contract
- Resource-oriented paths (`/users/{id}/orders`); verbs via HTTP methods.
- Consistent casing and envelope across all endpoints; document with OpenAPI.
- Backward-compatible changes only within a version; breaking → new version.

## Requests & responses
- Validate and coerce inputs; reject unknown fields explicitly or ignore by policy.
- Paginate collections (cursor preferred); include total/next where cheap.
- Use precise status codes: 400 validation, 401 authn, 403 authz, 404, 409, 422.

## Auth & safety
- Authenticate every non-public route; authorize per-resource, not just per-route.
- Rate-limit and validate size of payloads. Never accept secrets in query strings.

## Errors
- Stable machine-readable error shape: `{ code, message, details? }`.
""",
    ),
    "testing": _s(
        "testing",
        "Testing strategy: pyramid, what to test, determinism, fixtures. Load before writing tests or test-heavy tasks.",
        ("test", "spec", "coverage", "tdd", "unit", "integration", "e2e",
         "vitest", "jest", "pytest", "playwright", "тест", "покрыти"),
        ("testing", "standards"),
        """
# testing

Industry-baseline testing approach. Refine with this project's tools.

## What to test
- Test behavior and contracts, not implementation details.
- Cover the unhappy paths: empty, error, boundary, concurrent, malformed input.
- One clear assertion focus per test; name = scenario + expected outcome.

## The pyramid
- Many fast unit tests, fewer integration, few end-to-end. Keep E2E for critical flows.
- Push logic into pure functions so it's unit-testable without mocks.

## Determinism
- No real time/network/randomness in tests: inject clock, stub I/O, seed RNG.
- Tests are independent and order-free; each sets up and tears down its own state.

## Quality
- A failing test must point at the cause from its name + message alone.
- Treat a flaky test as a bug: quarantine and fix, don't retry-loop.
""",
    ),
    "security": _s(
        "security",
        "Security best practices: input handling, secrets, authz, OWASP basics. Load for auth, secrets, or user data.",
        ("security", "auth", "authn", "authz", "login", "password", "token",
         "secret", "credential", "encrypt", "permission", "session", "oauth",
         "jwt", "xss", "csrf", "injection", "безопасн", "пароль", "токен",
         "секрет", "доступ", "шифр"),
        ("security",),
        """
# security

Industry-baseline security. Refine with this project's threat model.

## Input & output
- Treat all external input as hostile: validate type, length, range, format.
- Escape/encode on output by context (HTML, SQL, shell, URL) — prevent injection/XSS.
- Use parameterized queries; never build SQL/commands by string concatenation.

## Secrets & auth
- Secrets only in env/secret store — never in code, logs, or the client bundle.
- Hash passwords with a slow KDF (bcrypt/argon2); never store plaintext.
- Authenticate then authorize per-resource; deny by default.
- Short-lived access tokens + rotation; scope them minimally.

## Data & transport
- TLS everywhere; set secure/httpOnly/sameSite on cookies.
- Log security events (authn failures, authz denials) without logging secrets/PII.

## OWASP reflex
- Check the change against: injection, broken access control, secrets exposure,
  SSRF, insecure deserialization, and misconfiguration.
""",
    ),
    "accessibility": _s(
        "accessibility",
        "Accessibility (WCAG) best practices: semantics, keyboard, contrast, ARIA. Load for any user-facing UI.",
        ("accessibility", "a11y", "wcag", "aria", "screen reader", "keyboard",
         "contrast", "доступн", "скринрид"),
        ("accessibility", "ui", "frontend"),
        """
# accessibility

Industry-baseline accessibility (WCAG 2.2 AA). Refine per project.

- Semantic HTML before ARIA; ARIA only to fill genuine gaps, never to fix bad markup.
- Full keyboard operability: tab order is logical, focus is always visible, no traps.
- Labels for every input; `alt` for meaningful images, empty `alt` for decorative.
- Contrast ≥ 4.5:1 (text), 3:1 (large text/UI). Never use color as the only signal.
- Respect `prefers-reduced-motion`; don't autoplay motion/audio.
- Announce async changes (live regions) for screen-reader users.
- Test with keyboard-only and a screen reader on the critical flows.
""",
    ),
    "performance": _s(
        "performance",
        "Performance best practices: measure-first, hot paths, caching, budgets. Load for perf-sensitive tasks.",
        ("performance", "perf", "latency", "throughput", "optimize",
         "optimization", "cache", "caching", "slow", "speed", "bundle size",
         "производительн", "оптимиз", "кэш", "латенс"),
        ("performance", "constraints"),
        """
# performance

Industry-baseline performance. Refine with this project's budgets.

- Measure before optimizing: profile to find the real hot path; don't guess.
- Fix algorithmic cost first (O(n²)→O(n)) before micro-optimizing.
- Avoid N+1 I/O: batch, prefetch, or join. Paginate large reads.
- Cache deliberately with explicit invalidation; a stale cache is a bug.
- Set budgets (latency p95, bundle KB, query count) and assert them in CI where possible.
- Defer/lazy-load non-critical work; do heavy work off the request path.
- Re-measure after the change to confirm the win and catch regressions.
""",
    ),
    "database": _s(
        "database",
        "Database best practices: schema, indexing, migrations, transactions. Load for data-model or query work.",
        ("database", "db", "sql", "schema", "migration", "index", "query",
         "postgres", "mysql", "sqlite", "mongo", "orm", "table",
         "база данных", "миграц", "индекс", "схема", "таблиц", "запрос"),
        ("database", "backend"),
        """
# database

Industry-baseline data practices. Refine with this project's engine.

## Schema
- Model the domain, then normalize; denormalize only for measured read needs.
- Constraints in the DB (NOT NULL, FK, UNIQUE) — the schema is the last line of defense.
- Explicit types and sane defaults; store time in UTC.

## Queries & indexing
- Index the columns you filter/join/sort on; verify with the query planner.
- Select only needed columns; never `SELECT *` in hot paths. Paginate.
- Watch for N+1; prefer set-based operations over per-row loops.

## Migrations & integrity
- Migrations are forward-only, reviewed, and reversible-by-design; test on a copy.
- Wrap multi-step writes in transactions; keep them short to avoid lock contention.
- Never run destructive migrations without a backup and a rollback plan.
""",
    ),
}


def _existing_names(env_dir: Path) -> set[str]:
    """Names of skills that already exist AND have real content (not an empty stub)."""
    present: set[str] = set()
    for s in skills_mod.discover(env_dir):
        body = s.body()
        # A stub is just the heading; require some substance to count as "covered".
        if len(body.splitlines()) >= 4 or len(body) >= 200:
            present.add(s.name.strip().lower())
    return present


def infer_domains(text: str) -> list[str]:
    """Return library domain names whose keywords appear in `text` (lowercased)."""
    t = (text or "").lower()
    hits: list[str] = []
    for name, lib in LIBRARY.items():
        if any(kw in t for kw in lib.keywords):
            hits.append(name)
    return hits


def gaps(env_dir: Path, task) -> list[LibrarySkill]:
    """Domains this task touches that have NO existing non-empty skill.

    A domain is considered covered if a skill with its name OR any of its
    aliases already exists with real content.
    """
    text = " ".join(
        [task.title or "", task.description or "", " ".join(task.skills or [])]
    )
    domains = set(infer_domains(text))
    # Task's explicit skill tags also count as domain signals.
    for tag in (task.skills or []):
        if tag.strip().lower() in LIBRARY:
            domains.add(tag.strip().lower())

    present = _existing_names(env_dir)
    missing: list[LibrarySkill] = []
    for name in domains:
        lib = LIBRARY[name]
        if name in present or any(a in present for a in lib.aliases):
            continue
        missing.append(lib)
    return missing


def install(env_dir: Path, name: str) -> Path | None:
    """Write the baseline SKILL.md for a library domain. Returns the path, or
    None if the name isn't in the library. Won't overwrite an existing skill
    that already has real content."""
    name = name.strip().lower()
    lib = LIBRARY.get(name)
    if lib is None:
        return None
    skill_dir = env_dir / "skills" / lib.name
    md = skill_dir / "SKILL.md"
    if md.exists():
        existing = skills_mod.read_skill(env_dir, lib.name) or ""
        if len(existing) >= 200:
            return md  # already substantive — leave it
    skill_dir.mkdir(parents=True, exist_ok=True)
    md.write_text(
        f"---\nname: {lib.name}\ndescription: {lib.description}\n---\n\n{lib.body}\n",
        encoding="utf-8",
    )
    return md


def gap_note(env_dir: Path, task) -> str:
    """A short prompt-injection note listing missing-domain skills for a task,
    or '' if none. Tells the agent how to fill the gap."""
    missing = gaps(env_dir, task)
    if not missing:
        return ""
    lines = [
        "## ⚠️ Skill gaps for this task",
        "This task touches domains with no project skill yet. Before "
        "implementing, bootstrap the baseline and then refine it with "
        "project-specifics (ask the human via `ask_user(skill=…)` for anything "
        "the baseline can't assume):",
    ]
    for lib in missing:
        lines.append(
            f"- **{lib.name}** — no skill found. Call "
            f"`ensure_skill(\"{lib.name}\")` to install best-practice baseline, "
            f"then `read_skill(\"{lib.name}\")`."
        )
    return "\n".join(lines)


def _changed_files(project_root: Path) -> list[str]:
    """Files touched since HEAD (staged + unstaged). Empty on any git error."""
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=project_root, capture_output=True, text=True, timeout=10,
        )
        names = [l.strip() for l in out.stdout.splitlines() if l.strip()]
        return names
    except Exception:
        return []


def reconcile_brief(env_dir: Path, project_root: Path, task) -> str:
    """Post-task skill reconciliation prompt (experience-driven learning).

    After a task is done, the agent compares what it just built against the
    existing skills, then: writes durable, confident conventions automatically
    (`save_to_skill`, prefixed `[auto]`), and asks the human about anything that
    is a debatable trade-off or project policy (`ask_user(skill=…)`). This is
    how harn's skills grow from its own work, not just from human answers.
    """
    changed = _changed_files(project_root)
    idx = skills_mod.index(env_dir)
    domains = ", ".join(LIBRARY.keys())
    files_block = (
        "\n".join(f"  - {f}" for f in changed[:40]) if changed
        else "  (no git diff detected — reason from the task description)"
    )
    return (
        "## 🧠 Reconcile skills with what you just built\n"
        f"Task: **{task.id} — {task.title}**\n\n"
        "Compare the work in this iteration against the project's skills and "
        "CLOSE THE GAPS so the next task starts smarter.\n\n"
        "Files changed:\n" + files_block + "\n\n"
        "Existing skills:\n" + idx + "\n\n"
        "Do this:\n"
        "1. List the durable conventions/patterns this task established "
        "(libraries chosen, file/naming patterns, error shapes, data formats, "
        "validation approach, gotchas you hit and how you solved them).\n"
        "2. For each one NOT already covered by a skill:\n"
        "   - **Confident & factual** (a convention the code now follows): call "
        "`save_to_skill(<skill>, \"[auto] <the convention>\")`. Use an existing "
        "skill name when it fits, else a domain name "
        f"({domains}) or a sensible new one.\n"
        "   - **A trade-off or policy needing human agreement** (security "
        "posture, a choice with real downsides): call `ask_user(question, "
        "skill=<skill>)` instead of writing it yourself.\n"
        "3. Enrich — if a skill exists but this task revealed a better practice, "
        "append it.\n"
        "4. **Codebase map** — if this task changed the structure, stack, a "
        "module's responsibility, or established a new in-code standard, update "
        "`harn_env/CODEBASE.md` via `update_codebase_map` (read it first, carry "
        "over what's still true).\n"
        "Keep entries short and reusable. Skip anything task-specific or obvious. "
        "If nothing durable was learned, say so and move on."
    )
