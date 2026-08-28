"""Synthetic stand-ins, so the harness itself is exercised without real records.

The real golden corpus is a set of genuine personal documents — a DD-214, a VA
medical record, a bad scan — and it cannot be generated. These fixtures verify
that the *scorer* works; the figure that clears the R-01 gate has to come from
the real thing.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CLEAN_SCAN = """DEPARTMENT OF DEFENSE
CERTIFICATE OF RELEASE OR DISCHARGE FROM ACTIVE DUTY
1. NAME Last First Middle
DEMERS MATTHEW
12. RECORD OF SERVICE
a. Date Entered AD This Period 2009 06 15
b. Separation Date This Period 2014 08 11
This page exists to exercise the OCR stage end to end."""


def render_text_page(
    text: str,
    destination: Path,
    *,
    size: tuple[int, int] = (1700, 2200),
    font_size: int = 30,
    rotation: float = 0.0,
    noise: int = 0,
) -> Path:
    """Render text as an image, optionally skewed and speckled.

    `rotation` and `noise` exist to make a deliberately bad scan, which is how
    the deskew and clean options get measured rather than assumed (REQ-013).
    """
    import random

    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=font_size)

    y = 160
    for line in text.splitlines():
        draw.text((140, y), line, font=font, fill=15)
        y += int(font_size * 2.0)

    if rotation:
        image = image.rotate(rotation, resample=Image.BICUBIC, fillcolor=255, expand=False)

    if noise:
        rng = random.Random(1234)
        pixels = image.load()
        for _ in range(noise):
            x = rng.randrange(size[0])
            y = rng.randrange(size[1])
            pixels[x, y] = rng.choice((0, 40, 90))

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
    return destination
