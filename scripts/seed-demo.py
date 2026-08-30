#!/usr/bin/env python3
"""Invented documents for the documentation screenshots.

    docker compose --env-file .env -f infra/docker-compose.yml exec api \
        python scripts/seed-demo.py

Documentation screenshots go **into the repository**, and this repository holds
a real archive's fixtures. A picture of somebody's discharge papers in the docs
would be a strange way to finish a phase about not reading other people's
documents — so the demo archive is four invented documents and a cast of Mum,
Dad and Sister, none of whom exist.

The PDFs are generated rather than committed: a handful of bytes of code beats
four binaries nobody can diff, and they carry real selectable text so OCR, the
known-form matcher and search all have something true to do.
"""

import asyncio
import io
import os

import sqlalchemy as sa

from api import ingest
from api.db.enums import ActorType, IngestSource
from api.db.models import AppUser, Library, Membership
from api.db.session import SessionFactory
from api.storage.blobs import store_stream

DEMO_EMAIL = os.environ.get("BINDERY_EMAIL", "demo@example.com")

PAGES = [
    ("Northwood Mutual — Statement", [
        "NORTHWOOD MUTUAL INSURANCE",
        "Policy Declarations",
        "Policy number: ****4417",
        "Effective 1 March 2024 to 1 March 2025",
        "Named insured: A. Placeholder",
        "Premium: $1,284.00 annually",
    ]),
    ("Fenwick Garage — Invoice", [
        "FENWICK GARAGE LTD",
        "Invoice 20241, 14 March 2024",
        "Front brake pads replaced, both sides",
        "Brake fluid flushed and replaced",
        "Labour 2.0 hours",
        "Total due: $412.60",
    ]),
    ("Harbour Utilities — Bill", [
        "HARBOUR UTILITIES",
        "Account ****9013",
        "Billing period: February 2024",
        "Electricity used: 812 kWh",
        "Amount due: $146.22 by 28 March 2024",
    ]),
    ("Certificate of Completion", [
        "CERTIFICATE OF COMPLETION",
        "Awarded to A. Placeholder",
        "Advanced Records Management",
        "Completed 9 June 2023",
        "Course reference AR-2231",
    ]),
]


def make_pdf(title, lines) -> bytes:
    """A minimal one-page PDF with real, selectable text."""
    text = "\n".join(f"({line}) Tj 0 -22 Td" for line in lines)
    content = f"BT /F1 13 Tf 60 720 Td\n{text}\nET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


async def one_chunk(data: bytes):
    yield data


async def main():
    async with SessionFactory() as session:
        user = (await session.execute(
            sa.select(AppUser).where(AppUser.email == DEMO_EMAIL)
        )).scalar_one()
        library = (await session.execute(
            sa.select(Library).join(Membership, Membership.library_id == Library.id)
            .where(Membership.user_id == user.id)
        )).scalars().first()
        print("library", library.name, library.id)

        for title, lines in PAGES:
            name = title.replace(" — ", " - ").replace(" ", "_") + ".pdf"
            blob = await store_stream(one_chunk(make_pdf(title, lines)))
            result = await ingest.register(
                session, blob, library_id=library.id,
                ingest_source=IngestSource.WEB_UPLOAD,
                original_filename=name,
                mime_type="application/pdf",
                actor_type=ActorType.HUMAN, actor_id=user.id,
            )
            print(name, "already there" if result.duplicate else "ingested")
        await session.commit()

asyncio.run(main())
