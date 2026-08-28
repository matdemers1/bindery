"""Bindery API.

Everything is mounted under /api so nginx can proxy a single prefix and the
Cloudflare Access service-token policy has one path to match (REQ-105).
"""

from fastapi import FastAPI

from api.routers import auth, documents, files, health, pipeline, search, segments, upload

app = FastAPI(
    title="Bindery",
    version="0.1.0",
    description="Self-hosted document archive with page-level retrieval.",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(upload.router, prefix="/api")
app.include_router(documents.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(files.router, prefix="/api")
app.include_router(segments.router, prefix="/api")
app.include_router(pipeline.router, prefix="/api")
