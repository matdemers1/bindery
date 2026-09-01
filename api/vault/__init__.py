"""The private vault (Phase 16, ADR-012).

Split deliberately: `crypto` knows nothing about the database, `store` knows
nothing about HTTP, and `session` holds the only key material in the process.
The order they were built in is the order they are safe to build in — the code
that destroys plaintext depends on the code that proves it can be read back.
"""
