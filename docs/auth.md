# Authentication

The host owns only **where the credential comes from on the wire**. The identity
*mechanism* is engine-side: every backend resolves a request to an engine
`AgentIdentity` (or `None`) and **never raises** for an unauthenticated request.
Backends are selected by config and are pluggable without touching the routes.

```
Authorization: Bearer <token>   ──▶  AuthBackend.resolve(request) ──▶ AgentIdentity | None
                                        │
                                        └─ strict mode + None → 401 (protected routes only)
```

## Selecting a backend

Set `PRISMAL_SERVER_HOST_AUTH_BACKEND` to one of:

| Backend | When to use | How it resolves identity |
|---|---|---|
| `none` (default) | Local dev — a fresh clone boots usable. | Always returns a fixed local `AgentIdentity` (`dev_identity_did` / `dev_identity_agent_name`). |
| `bearer` | Simple deployments with a static token map. | `Authorization: Bearer <token>` → DID via a configured `token → DID` map. Unknown/absent token → `None`. |
| `oidc` | Real IdP-issued JWTs. | Delegates token validation + identity minting to the engine's `OidcIdentityProvider` — **no token crypto in the host**. |

## Strict mode

`PRISMAL_SERVER_HOST_AUTH_STRICT=true` makes a request that resolves to `None`
get **`401`** on protected (chat/thread) routes. Health routes stay public. In
non-strict mode an unauthenticated request proceeds with `identity = None` (the
default local identity for `none`).

> A2A strictness is **not** enforced by the host. The host passes the resolved
> identity to `A2AServerHandler`, which owns its own strict gate (JSON-RPC
> `-32001`). See [a2a.md](./a2a.md).

## Bearer example

```bash
export PRISMAL_SERVER_HOST_AUTH_BACKEND=bearer
# Static token map is SecretStr by construction (raw tokens never surface in
# logs or responses). Configure via env/.env, e.g.:
#   PRISMAL_SERVER_BEARER_STATIC_TOKENS='{"s3cr3t":"did:key:zAcme"}'
```

```bash
curl -N -X POST localhost:8000/threads/t-1/messages \
  -H 'authorization: Bearer s3cr3t' \
  -H 'content-type: application/json' \
  -d '{"content":"hi"}'
```

## Writing a custom backend

Implement the `AuthBackend` protocol (`async resolve(request) -> AgentIdentity |
None`, must not raise) and register it in `build_auth_backend`. Because routes
depend only on the protocol, no route code changes.

The single wire credential is `Authorization: Bearer` (or none in dev); clients
need no other prismal-specific header beyond the optional `X-Org-Id` for
[tenancy](./tenancy.md).
