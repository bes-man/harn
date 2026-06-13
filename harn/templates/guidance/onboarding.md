---
topic: onboarding
summary: Bring harn up to speed on a new/under-specified project before building.
---

# Onboarding a new / under-specified project

harn is useless until it knows the project. If `harn_env/` is sparse — empty
`prd/`, skills are stubs, no `[feedback] test_cmd` — **onboard first**, as a
calm one-question-at-a-time dialog (never a questionnaire dump):

0. **Read `harn_env/state/ONBOARD.md`** if present (`harn onboard` writes it):
   the auto-detected stack and brief. Read the repo README/docs and use code
   search to map the code. The user may point you at md files — read those
   instead of asking from scratch.
1. **Register services** — for each service/module you understand, call
   `save_service(name, responsibility, content)`. This is the project's
   persistent AS-IS memory.
2. **What are we building?** Capture into a PRD (`harn_env/prd/<slug>.md`:
   Problem / Goal / Scope / Acceptance criteria). Confirm with the user.
3. **What standards apply?** Ask the ones that shape decisions — security,
   testing, frontend, API, code style — each as `ask_user(question,
   skill="<that skill>")` so the answer saves into the skill automatically (or
   `save_to_skill` when you discover a convention in the code).
4. **How do we verify?** Get the test command → set `[feedback] test_cmd`. For
   a web UI, fill `[browser] enabled/app_cmd/app_url` and re-run `harn setup`.

Don't start building until the PRD + key skills are filled and confirmed.
