# Set the project's test command

status: todo
priority: 10

## What
Configure `[feedback] test_cmd` in harn_env/harn.toml so the loop has a real
feedback signal (e.g. `pytest -q`, `npm test`, `go test ./...`).

## Done when
- test_cmd is set and `harn run` executes it after each turn.
