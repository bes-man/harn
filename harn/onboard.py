"""Onboarding an existing (brownfield) project.

harn is useless to the agent until it knows the project. Onboarding:
  1. detects the stack from marker files (no LLM),
  2. seeds the skills with the obvious facts it found,
  3. warms the code-search index (semble / SocratiCode) so RAG is ready,
  4. hands the agent a brief to finish via a structured dialog (and to read any
     md docs the user points at instead of answering from scratch).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import skills as skills_mod


# (marker file, language, package-manager) — first match wins per language.
_MARKERS = [
    ("pyproject.toml", "Python", "pip/uv/poetry"),
    ("requirements.txt", "Python", "pip"),
    ("setup.py", "Python", "pip"),
    ("package.json", "JavaScript/TypeScript", "npm/pnpm/yarn"),
    ("go.mod", "Go", "go modules"),
    ("Cargo.toml", "Rust", "cargo"),
    ("pom.xml", "Java", "maven"),
    ("build.gradle", "Java/Kotlin", "gradle"),
    ("Gemfile", "Ruby", "bundler"),
    ("composer.json", "PHP", "composer"),
]

# substring in deps/files → framework label
_FRAMEWORK_HINTS = {
    "react": "React", "next": "Next.js", "vue": "Vue", "svelte": "Svelte",
    "angular": "Angular", "express": "Express", "fastify": "Fastify",
    "nestjs": "NestJS", "django": "Django", "flask": "Flask",
    "fastapi": "FastAPI", "starlette": "Starlette", "rails": "Rails",
    "spring": "Spring",
}
_TEST_HINTS = {"pytest": "pytest", "jest": "jest", "vitest": "vitest",
               "mocha": "mocha", "go test": "go test", "cargo test": "cargo test"}
_LINT_HINTS = {"ruff": "ruff", "black": "black", "flake8": "flake8",
               "mypy": "mypy", "eslint": "eslint", "prettier": "prettier"}


def _read(path: Path, limit: int = 20000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit].lower()
    except OSError:
        return ""


def detect_stack(project_root: Path) -> dict:
    """Best-effort, no-LLM stack detection from marker files + their contents."""
    langs: list[str] = []
    package_manager = ""
    blob = ""  # concatenated dependency text to scan for hints
    for marker, lang, pm in _MARKERS:
        p = project_root / marker
        if p.exists():
            if lang not in langs:
                langs.append(lang)
                package_manager = package_manager or pm
            blob += "\n" + _read(p)
            if marker == "package.json":
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    blob += " " + " ".join(
                        list((d.get("dependencies") or {}))
                        + list((d.get("devDependencies") or {})))
                except Exception:
                    pass

    frameworks = sorted({v for k, v in _FRAMEWORK_HINTS.items() if k in blob})
    tests = sorted({v for k, v in _TEST_HINTS.items() if k in blob})
    linters = sorted({v for k, v in _LINT_HINTS.items() if k in blob})

    # A rough size signal
    code_files = sum(1 for _ in project_root.rglob("*.py")) \
        + sum(1 for _ in project_root.rglob("*.ts")) \
        + sum(1 for _ in project_root.rglob("*.js")) \
        + sum(1 for _ in project_root.rglob("*.go"))

    docs = [p.name for p in project_root.glob("*.md")][:10]
    return {
        "languages": langs,
        "package_manager": package_manager,
        "frameworks": frameworks,
        "tests": tests,
        "linters": linters,
        "code_files": code_files,
        "docs": docs,
    }


def seed_skills(env_dir: Path, stack: dict) -> list[str]:
    """Write the obvious detected facts into skills (B2). Returns notes added."""
    notes: list[str] = []
    if stack["languages"]:
        fact = (f"Stack: {', '.join(stack['languages'])}"
                + (f" ({stack['package_manager']})" if stack['package_manager'] else ""))
        if stack["frameworks"]:
            fact += f"; frameworks: {', '.join(stack['frameworks'])}"
        skills_mod.append_learning(env_dir, "project", fact + " — auto-detected.")
        notes.append(fact)
    if stack["tests"]:
        skills_mod.append_learning(
            env_dir, "standards",
            f"Test framework: {', '.join(stack['tests'])} — auto-detected.")
        notes.append("tests: " + ", ".join(stack["tests"]))
    if stack["linters"]:
        skills_mod.append_learning(
            env_dir, "standards",
            f"Lint/format: {', '.join(stack['linters'])} — auto-detected.")
        notes.append("lint: " + ", ".join(stack["linters"]))
    return notes


_ONBOARD_BRIEF = """\
## ONBOARDING — build the project picture before any task

harn knows nothing about this project yet, and is useless until it does. Do this
as a structured dialog (one question at a time) — do not invent answers.

**How to ask every question** (three steps, same turn):
1. Show it to the human via the native `AskUserQuestion` tool (interactive
   option buttons in Claude Code). No such tool → write it as plain chat text.
2. Persist it with harn's `ask_user(question, skill=…)`.
3. After the human answers, record it with `answer_question(answer=…)`.
Never rely on the `ask_user` tool call alone — its arguments are collapsed in
the chat UI and the human will not see the question.

1. **Read what exists first** (cheap before asking): the auto-detected stack
   above, the repo's README / docs ({docs}), and use code search
   (`search` / `codebase_search`) to map the codebase. The human may point you
   at md files with project info — read those instead of asking from scratch.
2. **Fill the PRD(s)** in `harn_env/prd/`: problem, goal, scope, constraints.
   Ask only what the docs/code don't answer.
3. **Capture standards** — for each of security, testing, frontend/UI, API,
   code style: ask `ask_user(question, skill="<that skill>")` so the answer is
   saved into the skill automatically. These drive every later decision.
4. **Set up verification** in `harn_env/harn.toml`:
   - the test command → `[feedback] test_cmd` (e.g. "pytest -q", "npm test");
   - if the project has a web UI: ask how to start it and where it answers,
     then fill `[browser] enabled/app_cmd/app_url` so harn can verify UI tasks
     in a real browser (Playwright MCP), and tell the human to re-run
     `harn setup` once so the Playwright MCP server is wired in.
5. **Confirm the picture** with the human, then create the first tasks
   (`create_task`).

Do not start implementation until the PRD + key skills are filled and confirmed.
"""


def onboard_brief(stack: dict) -> str:
    docs = ", ".join(stack.get("docs") or []) or "none found"
    return _ONBOARD_BRIEF.format(docs=docs)


def stack_summary(stack: dict) -> str:
    if not stack["languages"] and not stack["code_files"]:
        return "no recognizable stack markers"
    parts = []
    if stack["languages"]:
        parts.append("languages: " + ", ".join(stack["languages"]))
    if stack["frameworks"]:
        parts.append("frameworks: " + ", ".join(stack["frameworks"]))
    if stack["tests"]:
        parts.append("tests: " + ", ".join(stack["tests"]))
    if stack["linters"]:
        parts.append("lint: " + ", ".join(stack["linters"]))
    parts.append(f"~{stack['code_files']} code files")
    if stack["docs"]:
        parts.append("docs: " + ", ".join(stack["docs"]))
    return "; ".join(parts)
