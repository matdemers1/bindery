# Cloudflare Tunnel + Access setup

See `D3 Cloud Vault/Bindery/ADR-006 — Cloudflare Tunnel and Access for Ingress.md`.

## Invariants

- **No service publishes a host port.** Verify with `make ps`: no row may show a
  `0.0.0.0:<host>-><container>` mapping. Bare entries like `5432/tcp` and `80/tcp`
  are the image's own `EXPOSE` declarations — container-internal, not published —
  and cannot be removed. The check that matters is the absence of `->`.
- **Application-level JWT auth exists regardless.** Access is defense in depth, never the perimeter.

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

## Programmatic API access (REQ-105)

Cloudflare Access expects a browser identity flow, which breaks scoped API tokens and any future native client.
Configure an Access **service token** and a policy that permits it on the `/api/` path, so a token can authenticate
without a browser. Verify with:

```bash
curl -H "CF-Access-Client-Id: <id>" -H "CF-Access-Client-Secret: <secret>" \
     https://$BINDERY_HOSTNAME/api/documents
```

Bindery's own auth layer already supports the token path: every endpoint accepts
either the HTTP-only `bindery_access` cookie or an `Authorization: Bearer <jwt>`
header, so Access is the only thing standing between a token and the API.

## Accepted risk

Cloudflare terminates TLS and can technically observe traffic containing medical and identity documents.
Documented in the Risk Register as **R-04**, with tripwire: *any change in sensitivity posture, or a decision to productize*.
