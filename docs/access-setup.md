# Cloudflare Tunnel setup

See `D3 Cloud Vault/Bindery/ADR-006 — Cloudflare Tunnel and Access for Ingress.md`,
then `ADR-008`, which removed the Access half on 2026-08-30.

> [!warning] Cloudflare Access is gone; the tunnel is the whole perimeter
> ADR-006 described Access as defence in depth in front of Bindery's own login.
> ADR-008 removed it, because Access expects a browser identity flow and that
> breaks scoped API tokens and any future native client (R-15). So the tunnel is
> now the *only* thing in front of the origin, `bindery.d3cloud.io` answers the
> open internet with Bindery's own login page, and `api/auth/throttle.py`, the
> password policy, TOTP and the nginx security headers are load-bearing rather
> than a second layer (invariant 9). Do not reinstate the Access application.

## Invariants

- **No service publishes a host port.** Verify with `make ps`: no row may show a
  `0.0.0.0:<host>-><container>` mapping. Bare entries like `5432/tcp` and `80/tcp`
  are the image's own `EXPOSE` declarations — container-internal, not published —
  and cannot be removed. The check that matters is the absence of `->`.
- **Application-level JWT auth is the perimeter**, not defence in depth. It was
  the second of two gates and is now the only one.

## Token-mode tunnel

The tunnel runs with `tunnel run --token`, so **ingress rules are configured in the
Cloudflare dashboard**, not in a config file in this repo. There is deliberately no
`infra/cloudflared/config.yml`; the single source of truth for routing is the
dashboard, and the only secret here is `CLOUDFLARE_TUNNEL_TOKEN` in `.env`.

Route both of these to the internal services:

| Hostname / path | Service |
|---|---|
| `$BINDERY_HOSTNAME/` | `http://web:80` |
| `$BINDERY_HOSTNAME/api/` | `http://web:80` (nginx proxies `/api/` to `api:8000`) |

The tunnel is held behind a compose profile so the stack runs without a token
during development:

```bash
make up       # no tunnel
make tunnel   # with ingress
```

## Programmatic API access (REQ-105, REQ-107)

Bindery's own scoped API tokens, and nothing else. Every endpoint accepts either
the HTTP-only `bindery_access` cookie or an `Authorization: Bearer <token>`
header; tokens are created in the UI, only their hash is stored, the secret is
shown once, and each one is re-intersected with its creator's memberships on
every request — so removing a membership shrinks every token immediately.

```bash
curl -H "Authorization: Bearer <token>" https://$BINDERY_HOSTNAME/api/documents
```

> **The `CF-Access-Client-Id` / `CF-Access-Client-Secret` pair this section used
> to describe no longer authenticates anything.** The Access service token in
> `.deploy/cloudflare.json` is dead. Requiring it was the reason Access was
> removed: a browser identity flow in front of a REST API is R-15.

## Accepted risk

Cloudflare terminates TLS and can technically observe traffic containing medical and identity documents.
Documented in the Risk Register as **R-04**, with tripwire: *any change in sensitivity posture, or a decision to productize*.
