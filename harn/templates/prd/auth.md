---
id: auth
status: active
priority: 10
---

# PRD: Auth System

## Problem
Users have no secure way to authenticate. Sessions are stateless and any
protected endpoint is currently open.

## Goal
Issue short-lived JWTs on login; verify them on protected routes; provide a
refresh flow so users stay logged in without re-entering credentials.

## Scope
### In scope
- POST /login → signed JWT (15 min)
- POST /refresh → rotate refresh token (7 days, stored in `sessions` table)
- Middleware: reject missing/expired tokens with 401

### Out of scope
- OAuth / social login
- Multi-factor authentication

## Acceptance criteria
- POST /login returns a signed JWT for valid credentials.
- Protected routes reject missing/expired tokens with 401.
- Refresh token is rotated on every use.
- JWT secret comes from env (`JWT_SECRET`), never hardcoded.

## Open questions
- Should refresh tokens be single-use or allow a small reuse window?
