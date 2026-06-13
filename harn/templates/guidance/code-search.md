---
topic: code-search
summary: semble + SocratiCode — search before reading files; saves ~98% of tokens vs reading.
---

# Code search (search before reading)

harn integrates two optional code-search backends. Use whichever is in your
tool list.

**SocratiCode** (static dependency graph — precise for impact analysis):
- `codebase_impact("symbol")` — what breaks if this symbol changes (blast radius)
- `codebase_symbol("symbol")` — definition + all callers + all callees
- `codebase_search("query")` — hybrid semantic+BM25 search over the whole repo

**semble** (semantic chunk retrieval — lightweight, no Docker). Tools named
`search` / `find_related` (client may prefix, e.g. `mcp__semble__search`):
- `search("topic")` → only the relevant chunks (~98% fewer tokens than reading
  files). Call before opening any file.
- `find_related("file.py", 42)` → semantic neighbours of a changed line.

**Protocol:**
1. Search first — never open a whole file when a search can narrow it down.
2. SocratiCode > semble for "what depends on X" (static is precise).
3. semble > manual grep for "find code similar to X".
4. Read full files only when you need context outside the returned chunks.

**Language note:** semantic search is tuned for English code identifiers. Even
if the task/PRD is in another language, search by the actual **code symbols**
(English identifiers), e.g. `verifyToken`, not «проверка токена».
`codebase_impact`/`codebase_symbol`/`find_related` are language-independent.
