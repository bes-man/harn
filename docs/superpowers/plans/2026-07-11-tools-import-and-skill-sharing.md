# Tools Single-Import + Skill Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Tools tab's two separate "＋ Upload tool" / "＋ Import tool" buttons with one "＋ Import" button that auto-detects which existing backend flow to use, and add an Export button to the skill editor so a skill can be shared with a colleague.

**Architecture:** Both changes are additive UI wiring on top of ALREADY-EXISTING, already-reviewed backend flows — Task 1 is pure client-side routing logic (no new backend route, no change to `tools.py`'s save/import_bundle security-reviewed code), Task 2 adds one small `GET` route mirroring the tools Export route's already-established pattern.

**Tech Stack:** Python stdlib only, vanilla JS in `harn/studio.py`'s inline `<script>` block, pytest.

## Global Constraints

- **Names in this plan are best-effort, not gospel.** Every prior UI task's brief in this repo's history has had at least one guessed JS function/variable name turn out wrong once the implementer actually read the current file (e.g. `switchTab` → the real name was `showTab`; `loadTools` → the real name was `loadToolsData`). Before using ANY name this plan cites, grep the ACTUAL current `harn/studio.py` to confirm it still exists with the signature shown here — this file has changed across 6 phases of work and line numbers drift.
- Do NOT change `harn/tools.py`'s `save`/`import_bundle`/`execute` or `harn/mcp_server.py`'s custom-tool registration — that code went through two security review rounds in Phase 5 (an import-bundle shell-exec vulnerability was found and fixed there) and must not be touched by this plan.
- No new build step, no new third-party dependencies — vanilla JS only, matching `harn/studio.py`'s existing style exactly.
- Every committed change bumps `__version__` in `harn/__init__.py` AND the version in `pyproject.toml` together (current version 0.17.41).

---

### Task 1: One "＋ Import" button for tools, auto-detecting bundle vs. script

**Files:**
- Modify: `harn/studio.py` — the custom-tools section's two buttons/inputs and their two handler functions (currently `renderCustomToolsSection`, `uploadToolFile`, `importToolFile` — grep to confirm these are still the real names and read their CURRENT bodies before editing; the code shown below is what they looked like as of commit 115c178, version 0.17.41)

**Interfaces:**
- Consumes: the EXISTING `/api/tools/save` (script-upload shape: `{name, description, params, command, source, script_name, content_b64}`) and `/api/tools/import` (bundle shape: `{filename, content_b64}`) routes — both unchanged, both already reviewed.
- Produces: nothing new for later tasks — this task is self-contained.

Today (grep-confirm before editing — line numbers will have drifted):

```javascript
function renderCustomToolsSection(){
  const rows=CUSTOM_TOOLS.map(t=>
    `<div class="skillrow"><div style="width:100%">`+
    `<div class="nm">${esc(t.name)} <span class="mut" style="font-weight:400">(${esc(t.source)})</span></div>`+
    `<div class="ds">${esc(t.description)}</div>`+
    `<button class="ghost" onclick="location.href=api('/api/tools/export?name='+encodeURIComponent('${esc(t.name)}'))" style="margin-top:4px">Export</button>`+
    `<button class="ghost" onclick="deleteCustomTool('${esc(t.name)}')" style="margin-top:4px">Delete</button>`+
    `</div></div>`).join('');
  return `<h2 style="margin-top:18px">CUSTOM TOOLS</h2>`+
    (rows||'<div class="empty">None yet.</div>')+
    `<button class="ghost" style="margin-top:8px" onclick="$('#toolUploadInput').click()">＋ Upload tool</button>`+
    `<input type="file" id="toolUploadInput" style="display:none" onchange="uploadToolFile(this)"/>`+
    `<button class="ghost" style="margin-top:8px" onclick="$('#toolImportInput').click()">＋ Import tool</button>`+
    `<input type="file" id="toolImportInput" style="display:none" onchange="importToolFile(this)"/>`;
}
async function uploadToolFile(input){
  const file=input.files&&input.files[0]; if(!file)return;
  const name=prompt('Tool name (a-z0-9_ only):'); if(!name){ input.value=''; return; }
  const description=prompt('Description:')||'';
  const paramsRaw=prompt('Comma-separated param names (or leave blank):')||'';
  const params=paramsRaw.split(',').map(s=>s.trim()).filter(Boolean);
  const dataUrl=await new Promise((res,rej)=>{
    const r=new FileReader(); r.onload=()=>res(r.result); r.onerror=rej; r.readAsDataURL(file);
  });
  const content_b64=dataUrl.split(',')[1]||'';
  const argList=params.map(p=>'{'+p+'}').join(' ');
  const r=await post_('/api/tools/save',{name,description,params,
    command:`bash ${file.name} ${argList}`.trim(), source:'upload', script_name:file.name, content_b64});
  if(!r.ok){ alert(r.error||'save failed'); return; }
  input.value='';
  await loadToolsData(true);
  renderTools();
  alert('Saved. This tool will be available to the agent starting its NEXT session — not the one currently running.');
}
async function importToolFile(input){
  const file=input.files&&input.files[0]; if(!file)return;
  const dataUrl=await new Promise((res,rej)=>{
    const r=new FileReader(); r.onload=()=>res(r.result); r.onerror=rej; r.readAsDataURL(file);
  });
  const content_b64=dataUrl.split(',')[1]||'';
  const r=await post_('/api/tools/import',{filename:file.name,content_b64});
  if(!r.ok){ alert(r.error||'import failed'); return; }
  input.value='';
  await loadToolsData(true);
  renderTools();
  alert('Imported "'+r.name+'". Available to the agent starting its next session.');
}
```

- [ ] **Step 1: Read the current file, confirm names**

Grep `harn/studio.py` for `renderCustomToolsSection`, `uploadToolFile`, `importToolFile`, `loadToolsData`, `renderTools`, `post_`. Read each in full. If any name/signature differs from what's shown above, use the REAL current version as your baseline for the edit below — do not blindly copy-paste this plan's snippet over different real code.

- [ ] **Step 2: Replace the two buttons with one, and merge the two handlers into one auto-detecting handler**

Replace the two-button block in `renderCustomToolsSection`'s return value:

```javascript
    `<button class="ghost" style="margin-top:8px" onclick="$('#toolUploadInput').click()">＋ Upload tool</button>`+
    `<input type="file" id="toolUploadInput" style="display:none" onchange="uploadToolFile(this)"/>`+
    `<button class="ghost" style="margin-top:8px" onclick="$('#toolImportInput').click()">＋ Import tool</button>`+
    `<input type="file" id="toolImportInput" style="display:none" onchange="importToolFile(this)"/>`;
```

with a single button/input:

```javascript
    `<button class="ghost" style="margin-top:8px" onclick="$('#toolImportInput').click()">＋ Import</button>`+
    `<input type="file" id="toolImportInput" style="display:none" onchange="importOrUploadToolFile(this)"/>`;
```

Replace the two separate async functions (`uploadToolFile`, `importToolFile`) with one that classifies the file first, then dispatches to whichever existing POST body shape applies:

```javascript
async function importOrUploadToolFile(input){
  const file=input.files&&input.files[0]; if(!file)return;
  const dataUrl=await new Promise((res,rej)=>{
    const r=new FileReader(); r.onload=()=>res(r.result); r.onerror=rej; r.readAsDataURL(file);
  });
  const content_b64=dataUrl.split(',')[1]||'';
  let isBundle=/\.zip$/i.test(file.name);
  if(!isBundle && /\.json$/i.test(file.name)){
    try{
      const text=atob(content_b64);
      const parsed=JSON.parse(text);
      isBundle = parsed && typeof parsed==='object' && 'name' in parsed && 'command' in parsed;
    }catch(e){ isBundle=false; }
  }
  if(isBundle){
    const r=await post_('/api/tools/import',{filename:file.name,content_b64});
    if(!r.ok){ alert(r.error||'import failed'); return; }
    input.value='';
    await loadToolsData(true);
    renderTools();
    alert('Imported "'+r.name+'". Available to the agent starting its next session.');
    return;
  }
  const name=prompt('Tool name (a-z0-9_ only):'); if(!name){ input.value=''; return; }
  const description=prompt('Description:')||'';
  const paramsRaw=prompt('Comma-separated param names (or leave blank):')||'';
  const params=paramsRaw.split(',').map(s=>s.trim()).filter(Boolean);
  const argList=params.map(p=>'{'+p+'}').join(' ');
  const r=await post_('/api/tools/save',{name,description,params,
    command:`bash ${file.name} ${argList}`.trim(), source:'upload', script_name:file.name, content_b64});
  if(!r.ok){ alert(r.error||'save failed'); return; }
  input.value='';
  await loadToolsData(true);
  renderTools();
  alert('Saved. This tool will be available to the agent starting its NEXT session — not the one currently running.');
}
```

(`atob` decodes the base64 content back to text for the JSON-sniff check — this only runs for `.json`-named files, so it never tries to `atob`/`JSON.parse` a `.zip` or a genuinely binary script; a `.json` file that fails to parse as JSON, or parses but lacks `name`+`command`, correctly falls through to the upload/script path, matching the spec's "backend is authoritative either way" framing — a mis-classified `.json` script would just get `/api/tools/save`'d as a script named `<whatever>.json`, harmless.)

- [ ] **Step 3: Verify JS syntax**

Run:
```bash
python3 -c "
import re
html = open('harn/studio.py').read()
m = re.search(r'<script>(.*)</script>', html, re.DOTALL)
open('/tmp/studio_check.js', 'w').write(m.group(1))
"
node --check /tmp/studio_check.js
```
Expected: no output (syntax OK).

- [ ] **Step 4: Manual live verification**

If a live-preview/browser MCP tool is available: start the studio dev server, open the Tools tab, click "＋ Import", pick a `.sh` script — confirm the name/description/params prompts appear and the tool is saved with `source: "upload"`. Then export a tool (Export button on an existing row) to get a real bundle file, and import THAT file via the same "＋ Import" button — confirm it's recognized as a bundle (no name/description/params prompts) and imported successfully. If no live tooling is available, verify via careful tracing instead and say so explicitly in your report.

- [ ] **Step 5: Run the full suite (sanity check — this task is JS-only)**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 590 passed, unchanged (no Python code touched).

- [ ] **Step 6: Bump version and commit**

Edit `harn/__init__.py`: bump `__version__` by one patch version from whatever it currently is (confirm the real current value first — do not assume 0.17.41 is still current if other work has landed since). Edit `pyproject.toml` to match.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml
git commit -m "feat(studio): single auto-detecting Import button for custom tools (Phase 7)"
```

---

### Task 2: Skill Export button + backend route

**Files:**
- Modify: `harn/studio.py` — `renderSkillEditor` (add an Export button) and the `do_GET` route table (add `/api/skill/export`)
- Test: wherever skill-related payload/route tests already live (grep for an existing test hitting `/api/skill` or a skills-payload function to find the right file — if none exists at the studio-route level, add to a new small test or the closest existing studio-route test file)

**Interfaces:**
- Consumes: `harn/skills.py`'s existing `read_skill(env_dir, name) -> str | None` (returns the skill's full body, frontmatter stripped) — for the EXPORT route you actually want the RAW file bytes (frontmatter included), so read the file directly via `harn/skills.py`'s `discover(env_dir) -> list[Skill]` (each `Skill` has a `.path: Path` field pointing at its real `SKILL.md` on disk) rather than `read_skill`, which strips frontmatter.
- Produces: `GET /api/skill/export?name=<name>` — streams that skill's raw `SKILL.md` bytes with `Content-Disposition: attachment; filename="<name>.md"`, or 404 if no skill has that name.

- [ ] **Step 1: Read the current file, confirm names**

Grep `harn/studio.py` for `renderSkillEditor`, the `/api/tools/export` route (the pattern to mirror), and `_send` (confirm whether it already has an `extra_headers` parameter — Phase 5's tools-export route added one; check its current signature). Grep `harn/skills.py` for `discover`/`Skill` to confirm the `Skill` dataclass still has a `.path` field.

- [ ] **Step 2: Write the failing test**

Find or create the right test file (grep `tools_catalog_payload`'s test file for the studio-route testing convention this project already uses, and match its `_env(tmp_path)` fixture style):

```python
def test_skill_export_route_streams_the_raw_skill_md(tmp_path):
    import urllib.request
    env, project_root = _env(tmp_path)  # match the real fixture helper's actual name/shape
    skills_mod.append_learning(env, "backend", "use FastAPI", description="Backend conventions")
    skill = skills_mod.discover(env)[0]
    raw_bytes = skill.path.read_bytes()
    # exercise this at whatever level the project's existing route tests already
    # use (a direct payload-function call if one exists, or a real HTTP request
    # against a started studio server if that's this project's established
    # pattern for GET-with-raw-bytes routes like /api/tools/export -- match
    # tests/test_tools_payload.py's or the export test for tools, whichever
    # already covers a similar GET route, rather than inventing a new harness)
```

(This step's exact test SHAPE depends on how `/api/tools/export` — the precedent this route mirrors — is ALREADY tested in this codebase; find that test first via grep and copy its harness pattern exactly, substituting skills for tools. Do not guess a new testing approach.)

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest <the test file> -k skill_export -v`
Expected: FAIL (404 or route-not-found, since `/api/skill/export` doesn't exist yet).

- [ ] **Step 4: Add the route**

In `harn/studio.py`'s `do_GET`, find the `/api/tools/export` branch (read it in full first) and add a sibling branch near it:

```python
            elif route == "/api/skill/export":
                name = self._query("name") or ""
                skill = next((s for s in skills_mod.discover(env) if s.name == name), None)
                if skill is None:
                    self._send(404, b"not found", "text/plain"); return
                data = skill.path.read_bytes()
                self._send(200, data, "text/markdown",
                          extra_headers={"Content-Disposition": f'attachment; filename="{name}.md"'})
```

(Confirm `_send`'s real signature accepts `extra_headers` — Phase 5's Task 7 added this parameter for the tools-export route; if it's spelled differently or doesn't exist yet, read `_send`'s actual current definition and either use its real parameter name or add the same kind of optional header-writing support Task 7 already added, matching that precedent exactly rather than inventing a new mechanism.)

- [ ] **Step 5: Add the Export button to the skill editor**

In `renderSkillEditor` (read its current body first), find the row with the Save button (currently something like):

```javascript
    <div class="row" style="margin-top:12px">
      <button class="primary" onclick="saveSkill(${skillSel})">Save skill</button>
      <span class="status" id="sst"></span>
    </div>
```

Add an Export button alongside it:

```javascript
    <div class="row" style="margin-top:12px">
      <button class="primary" onclick="saveSkill(${skillSel})">Save skill</button>
      <button class="ghost" onclick="location.href=api('/api/skill/export?name='+encodeURIComponent(s.name))">Export</button>
      <span class="status" id="sst"></span>
    </div>
```

(`s` is already in scope in `renderSkillEditor` as `const s=S.skills[skillSel]` — confirm this variable name in the real current function body before using it.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest <the test file> -k skill_export -v`
Expected: PASS.

- [ ] **Step 7: Verify JS syntax**

Run the same `node --check` extraction as Task 1's Step 3.

- [ ] **Step 8: Manual live verification**

If live-preview tooling is available: open the Skills tab, click a skill, click the new "Export" button, confirm a `.md` file downloads containing the skill's real frontmatter + body. If not available, rely on tracing and say so explicitly.

- [ ] **Step 9: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 590 (or whatever the real current baseline is — confirm before this task) + 1 new test.

- [ ] **Step 10: Bump version and commit**

Bump `__version__`/`pyproject.toml` by one more patch version from Task 1's landed value.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml <test file>
git commit -m "feat(studio): skill Export button + /api/skill/export route (Phase 7)"
```
