# Cloudflare Tunnel + Access setup

See `D3 Cloud Vault/Bindery/ADR-006 — Cloudflare Tunnel and Access for Ingress.md`.

## Invariants

- **No service publishes a host port.** Verify with `docker compose ps` — the PORTS column must be empty for every service.
- **Application-level JWT auth exists regardless.** Access is defense in depth, never the perimeter.

## Programmatic API access (REQ-105)

Cloudflare Access expects a browser identity flow, which breaks scoped API tokens and any future native client.
Configure an Access **service token** and a policy that permits it on the `/api/` path, so a token can authenticate
without a browser. Verify with:

```bash
curl -H "CF-Access-Client-Id: <id>" -H "CF-Access-Client-Secret: <secret>" \
     https://$BINDERY_HOSTNAME/api/documents
```

## Accepted risk

Cloudflare terminates TLS and can technically observe traffic containing medical and identity documents.
Documented in the Risk Register as **R-04**, with tripwire: *any change in sensitivity posture, or a decision to productize*.
