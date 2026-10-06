"""The synthetic OCR corpus — a measured figure CI can run (BND-T-005, REQ-018).

The real golden corpus is the maintainer's DD-214, VA records and statements; it
is never committed (BND-ADR-014), so a CI runner can never score it. This is the
corpus that *can* run anywhere: invented documents in the shapes the archive
actually holds — a typewritten separation form, a dense medical note, a bill, a
two-column statement, a deed, a W-2, a letter full of dates — each rendered and
then damaged the way scanners, faxes and photocopiers damage paper.

**Nothing here is real.** Every name, number, address and date is invented; the
people are "Example", the addresses are on streets that do not exist, and the
identifiers are the documentation ranges (SSNs in 000-, phone numbers 555-01xx).

Ground truth is exact by construction: `expected` is the text that was drawn, in
the order it was drawn, so there is no transcription to get wrong. The pages are
rendered at test time from this file — text and a seeded recipe, not binaries —
for the same reason `scripts/seed-demo.py` generates its PDFs: a diff of a
fixture should be readable.

What this measures is the OCR *stage* on known damage. It does not clear R-01:
real scans have failure modes nobody thought to synthesize, which is why
`test_golden_corpus_word_accuracy` still exists for the real corpus.
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

DPI = 200
PAGE = (1700, 2200)  # US Letter at 200 DPI
MARGIN = 130

_FONT_DIRS = (
    Path("/usr/share/fonts/truetype/liberation2"),
    Path("/usr/share/fonts/truetype/liberation"),
    Path("/usr/share/fonts/truetype/dejavu"),
)


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for directory in _FONT_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    # Outside the worker image the named face may be missing. Pillow's bundled
    # face keeps the harness importable, but the figure is only comparable run
    # to run inside the image, which is where CI measures it.
    return ImageFont.load_default(size=size)


@dataclass(frozen=True)
class Block:
    """A run of lines drawn in one face. `columns` splits it side by side."""

    lines: tuple[str, ...]
    font: str = "LiberationSans-Regular.ttf"
    size: int = 30
    columns: int = 1
    rule_after: bool = False
    boxed: bool = False


@dataclass(frozen=True)
class Damage:
    rotation: float = 0.0  # degrees, as a crooked feed
    speckle: int = 0  # dark specks, as platen dust
    blur: float = 0.0  # Gaussian radius, as a soft focus
    ink: int = 15  # text grey level; higher is fainter
    paper: int = 255  # background grey level
    downsample: float = 1.0  # < 1 renders low-resolution and scales back up, as a fax
    jpeg_quality: int | None = None  # a lossy re-save, as a phone or email scan
    threshold: int | None = None  # binarize, as a photocopier


@dataclass(frozen=True)
class Fixture:
    name: str
    why: str
    blocks: tuple[Block, ...]
    damage: Damage = field(default_factory=Damage)
    seed: int = 1

    @property
    def expected(self) -> str:
        """The ground truth: every drawn line, in reading order."""
        # Lines are listed in reading order, and a multi-column block is filled
        # column-major (`_split_columns`), so the left column comes first.
        return "\n".join(line for block in self.blocks for line in block.lines)


def _split_columns(lines: tuple[str, ...], columns: int) -> list[list[str]]:
    per = -(-len(lines) // columns)
    return [list(lines[i : i + per]) for i in range(0, len(lines), per)]


def render(fixture: Fixture, destination: Path) -> Path:
    """Draw the fixture, damage it, and save it as the scanner would have."""
    damage = fixture.damage
    rng = random.Random(fixture.seed)

    image = Image.new("L", PAGE, damage.paper)
    draw = ImageDraw.Draw(image)
    y = MARGIN
    width = PAGE[0] - 2 * MARGIN

    for block in fixture.blocks:
        font = _font(block.font, block.size)
        leading = int(block.size * 1.45)
        top = y
        if block.columns == 1:
            for line in block.lines:
                draw.text(
                    (MARGIN + (20 if block.boxed else 0), y), line, font=font, fill=damage.ink
                )
                y += leading
        else:
            gutter = 70
            column_width = (width - gutter * (block.columns - 1)) // block.columns
            bottom = y
            for index, column in enumerate(_split_columns(block.lines, block.columns)):
                x = MARGIN + index * (column_width + gutter)
                cy = y
                for line in column:
                    draw.text((x, cy), line, font=font, fill=damage.ink)
                    cy += leading
                bottom = max(bottom, cy)
            y = bottom
        if block.boxed:
            draw.rectangle((MARGIN, top - 12, MARGIN + width, y + 4), outline=damage.ink, width=2)
            y += 16
        if block.rule_after:
            draw.line((MARGIN, y + 6, MARGIN + width, y + 6), fill=damage.ink, width=3)
            y += 22
        y += int(block.size * 0.6)

    if damage.downsample < 1.0:
        small = (int(PAGE[0] * damage.downsample), int(PAGE[1] * damage.downsample))
        image = image.resize(small, Image.BILINEAR).resize(PAGE, Image.NEAREST)
    if damage.blur:
        image = image.filter(ImageFilter.GaussianBlur(damage.blur))
    if damage.rotation:
        image = image.rotate(
            damage.rotation, resample=Image.BICUBIC, fillcolor=damage.paper, expand=False
        )
    if damage.speckle:
        pixels = image.load()
        for _ in range(damage.speckle):
            x, y = rng.randrange(PAGE[0]), rng.randrange(PAGE[1])
            pixels[x, y] = rng.choice((0, 40, 90))
    if damage.threshold is not None:
        cut = damage.threshold
        image = image.point(lambda value: 0 if value < cut else 255)

    destination.parent.mkdir(parents=True, exist_ok=True)
    if damage.jpeg_quality is not None:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=damage.jpeg_quality)
        image = Image.open(io.BytesIO(buffer.getvalue()))
        destination = destination.with_suffix(".jpg")
        image.save(destination, format="JPEG", quality=95, dpi=(DPI, DPI))
    else:
        destination = destination.with_suffix(".png")
        image.save(destination, dpi=(DPI, DPI))
    return destination


MONO = "LiberationMono-Regular.ttf"
SERIF = "LiberationSerif-Regular.ttf"
SANS = "LiberationSans-Regular.ttf"
DEJAVU = "DejaVuSans.ttf"

FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        name="dd214-typewritten",
        why="The document the product is measured on: a typewritten separation form in boxes.",
        blocks=(
            Block(
                ("CERTIFICATE OF RELEASE OR DISCHARGE FROM ACTIVE DUTY",),
                font=SANS,
                size=34,
                rule_after=True,
            ),
            Block(("1. NAME (Last, First, Middle)", "EXAMPLE JORDAN AVERY"), font=MONO, boxed=True),
            Block(("2. DEPARTMENT, COMPONENT AND BRANCH", "ARMY RA"), font=MONO, boxed=True),
            Block(("3. SOCIAL SECURITY NUMBER", "000-12-3456"), font=MONO, boxed=True),
            Block(("4a. GRADE, RATE OR RANK  b. PAY GRADE", "SGT  E5"), font=MONO, boxed=True),
            Block(("5. DATE OF BIRTH (YYYYMMDD)", "19870314"), font=MONO, boxed=True),
            Block(
                (
                    "12. RECORD OF SERVICE  YEAR(S)  MONTH(S)  DAY(S)",
                    "a. DATE ENTERED AD THIS PERIOD  2009  06  15",
                    "b. SEPARATION DATE THIS PERIOD  2014  08  11",
                    "c. NET ACTIVE SERVICE THIS PERIOD  0005  01  27",
                ),
                font=MONO,
                boxed=True,
            ),
            Block(
                (
                    "13. DECORATIONS, MEDALS, BADGES, CITATIONS AND CAMPAIGN",
                    "RIBBONS AWARDED OR AUTHORIZED",
                    "ARMY COMMENDATION MEDAL  ARMY ACHIEVEMENT MEDAL 2ND AWARD",
                    "ARMY GOOD CONDUCT MEDAL  NATIONAL DEFENSE SERVICE MEDAL",
                ),
                font=MONO,
                boxed=True,
            ),
            Block(
                ("23. TYPE OF SEPARATION", "DISCHARGE", "24. CHARACTER OF SERVICE", "HONORABLE"),
                font=MONO,
                boxed=True,
            ),
        ),
        damage=Damage(rotation=0.7, speckle=2500, blur=0.6, ink=35, paper=242),
        seed=214,
    ),
    Fixture(
        name="va-medical-dense",
        why="Dense, small, high-stakes prose: a clinic note at 9pt.",
        blocks=(
            Block(("PRIMARY CARE CLINIC NOTE",), font=DEJAVU, size=30, rule_after=True),
            Block(
                (
                    "Patient: EXAMPLE, JORDAN A    Date of visit: 03/22/2021",
                    "Provider: R. Sample, MD    Location: Northfield Clinic, Room 4",
                ),
                font=DEJAVU,
                size=24,
            ),
            Block(
                (
                    "SUBJECTIVE: Veteran presents for follow up of chronic low back pain",
                    "and bilateral knee pain, onset during service in 2011 after a vehicle",
                    "rollover. Reports pain of 5 out of 10 at rest and 7 out of 10 with",
                    "prolonged standing. Sleep is interrupted two to three times nightly.",
                    "Denies numbness, weakness, bowel or bladder changes. Continues the",
                    "home exercise program three times weekly with partial relief.",
                    "OBJECTIVE: Blood pressure 128/82, pulse 71, temperature 98.4 F.",
                    "Lumbar flexion limited to 60 degrees with pain at end range. Straight",
                    "leg raise negative bilaterally. Knees without effusion; crepitus on",
                    "the right with flexion beyond 90 degrees. Gait is steady and normal.",
                    "ASSESSMENT: 1. Lumbosacral strain, chronic, service connected.",
                    "2. Patellofemoral pain syndrome, bilateral. 3. Insomnia secondary",
                    "to pain. Condition is stable compared with the visit of 09/14/2020.",
                    "PLAN: Continue naproxen 500 mg twice daily with food. Refer to",
                    "physical therapy for eight sessions. Sleep hygiene handout given.",
                    "Return to clinic in six months or sooner if symptoms worsen.",
                ),
                font=DEJAVU,
                size=24,
            ),
        ),
        damage=Damage(blur=0.5, speckle=600, ink=25, paper=248),
        seed=31,
    ),
    Fixture(
        name="utility-bill-clean",
        why="The easy case that must never regress: a clean modern bill.",
        blocks=(
            Block(("Riverside Power and Light",), font=SANS, size=40),
            Block(
                (
                    "Account number 4471 0093 2285    Statement date October 3, 2025",
                    "Service address 18 Example Lane, Northfield, ME 04000",
                ),
                font=SANS,
                size=28,
                rule_after=True,
            ),
            Block(
                (
                    "Previous balance  $112.40",
                    "Payment received September 18  -$112.40",
                    "Electric delivery charge 642 kWh  $58.19",
                    "Electric supply charge 642 kWh  $49.76",
                    "Customer charge  $10.50",
                    "Total amount due  $118.45",
                    "Payment due date October 27, 2025",
                ),
                font=SANS,
                size=30,
                rule_after=True,
            ),
            Block(
                (
                    "Questions about your bill? Call 207-555-0142 Monday through Friday.",
                    "Pay online, by phone, or by mail using the coupon below.",
                ),
                font=SANS,
                size=26,
            ),
        ),
        seed=3,
    ),
    Fixture(
        name="bad-scan",
        why="The quality floor: crooked, speckled, faint and soft all at once.",
        blocks=(
            Block(("NOTICE OF PROPERTY TAX ASSESSMENT",), font=SERIF, size=36, rule_after=True),
            Block(
                (
                    "Owner of record: Jordan A. Example",
                    "Parcel 012-047-0003    Tax year 2024",
                    "Land value $64,300    Building value $211,800",
                    "Total assessed value $276,100",
                    "Exemptions: homestead $25,000    veteran $6,000",
                    "Net taxable value $245,100",
                    "Mill rate 14.85    Tax due $3,639.74",
                    "First half due September 30    Second half due March 31",
                    "Appeals must be filed in writing within 185 days of commitment.",
                ),
                font=SERIF,
                size=32,
            ),
        ),
        damage=Damage(rotation=2.2, speckle=9000, blur=0.9, ink=95, paper=228),
        seed=13,
    ),
    Fixture(
        name="statement-two-column",
        why="Layout robustness: a two-column statement must be read a column at a time.",
        blocks=(
            Block(
                ("Northfield Federal Credit Union  Member Statement",),
                font=SERIF,
                size=34,
                rule_after=True,
            ),
            Block(
                (
                    "Your share savings account earned",
                    "dividends at an annual percentage",
                    "yield of 0.25 percent during this",
                    "statement period. Dividends are",
                    "credited on the last business day",
                    "of each month and compound daily.",
                    "Members who enroll in electronic",
                    "statements before December 31 are",
                    "entered in a drawing for a waiver",
                    "of one year of checking fees. Visit",
                    "any branch or call the member line",
                    "for details about the drawing.",
                ),
                font=SERIF,
                size=28,
                columns=2,
            ),
        ),
        damage=Damage(speckle=800, blur=0.4, ink=30),
        seed=2,
    ),
    Fixture(
        name="deed-photocopy",
        why="Known-form registry, vital tier: dense legal serif through a photocopier.",
        blocks=(
            Block(("WARRANTY DEED",), font=SERIF, size=40, rule_after=True),
            Block(
                (
                    "KNOW ALL PERSONS BY THESE PRESENTS that Morgan Sample and Casey",
                    "Sample, of Northfield, County of Penobscot, State of Maine, for",
                    "consideration paid, grant to Jordan A. Example, of Northfield,",
                    "with WARRANTY COVENANTS, the land in Northfield, County of",
                    "Penobscot, State of Maine, bounded and described as follows:",
                    "Beginning at an iron pin on the westerly side of Example Lane,",
                    "said pin marking the northeasterly corner of land now or formerly",
                    "of Taylor Placeholder; thence westerly along said Placeholder land",
                    "two hundred and ten feet to a stone wall; thence northerly along",
                    "said wall one hundred and fifty feet to a drill hole; thence",
                    "easterly two hundred and ten feet to said Lane; thence southerly",
                    "along said Lane one hundred and fifty feet to the point of beginning.",
                    "Being the same premises conveyed to the grantors by deed dated",
                    "June 2, 2004 and recorded in Book 9412, Page 118.",
                ),
                font=SERIF,
                size=28,
            ),
        ),
        damage=Damage(rotation=-0.5, speckle=3000, blur=0.8, ink=20, paper=236, threshold=150),
        seed=1918,
    ),
    Fixture(
        name="w2-numbers",
        why="Numbers are what a W-2 is for, and OCR confuses digits differently from words.",
        blocks=(
            Block(("Form W-2 Wage and Tax Statement 2023",), font=SANS, size=34, rule_after=True),
            Block(
                ("a Employee's social security number", "000-98-7654"),
                font=SANS,
                size=26,
                boxed=True,
            ),
            Block(
                ("b Employer identification number (EIN)", "00-1234567"),
                font=SANS,
                size=26,
                boxed=True,
            ),
            Block(
                (
                    "c Employer's name, address, and ZIP code",
                    "Placeholder Logistics LLC",
                    "400 Sample Road, Northfield, ME 04000",
                ),
                font=SANS,
                size=26,
                boxed=True,
            ),
            Block(
                ("1 Wages, tips, other compensation", "58214.77"), font=SANS, size=26, boxed=True
            ),
            Block(("2 Federal income tax withheld", "6305.12"), font=SANS, size=26, boxed=True),
            Block(("3 Social security wages", "61040.00"), font=SANS, size=26, boxed=True),
            Block(("4 Social security tax withheld", "3784.48"), font=SANS, size=26, boxed=True),
            Block(("5 Medicare wages and tips", "61040.00"), font=SANS, size=26, boxed=True),
            Block(("6 Medicare tax withheld", "885.08"), font=SANS, size=26, boxed=True),
        ),
        damage=Damage(speckle=1500, blur=0.5, ink=30, paper=245, jpeg_quality=55),
        seed=2023,
    ),
    Fixture(
        name="letter-several-dates",
        why="Date-priority rules need every date read: a letter that carries five of them.",
        blocks=(
            Block(
                (
                    "Department of Veterans Affairs",
                    "Regional Office, 1 Placeholder Plaza, Northfield, ME 04000",
                    "November 12, 2019",
                ),
                font=SERIF,
                size=28,
                rule_after=True,
            ),
            Block(
                (
                    "Dear Mr. Example:",
                    "We received your claim on August 30, 2019. We considered the",
                    "service treatment records for the period of June 15, 2009 through",
                    "August 11, 2014, and the examination completed on October 4, 2019.",
                    "Your combined evaluation is 40 percent, effective September 1, 2019.",
                    "Your first payment will be issued on or about December 1, 2019.",
                    "If you disagree with this decision, you have one year from the date",
                    "of this letter to request a review.",
                    "Sincerely,",
                    "Veterans Service Center Manager",
                ),
                font=SERIF,
                size=28,
            ),
        ),
        damage=Damage(rotation=0.4, speckle=1200, ink=25),
        seed=1112,
    ),
    Fixture(
        name="fax-low-resolution",
        why="A faxed page: rendered at 100 DPI, scaled back up, and saved lossily.",
        blocks=(
            Block(("FAX TRANSMITTAL  Page 1 of 1",), font=SANS, size=34, rule_after=True),
            Block(
                (
                    "To: Records Department    From: Northfield Orthopedics",
                    "Re: Jordan A. Example    Date of service 02/09/2022",
                    "Please find the requested operative report attached. The",
                    "procedure was a right knee arthroscopy with partial medial",
                    "meniscectomy. There were no complications. The patient was",
                    "discharged the same day in stable condition with crutches and",
                    "instructions to bear weight as tolerated.",
                    "Follow up in two weeks for suture removal.",
                ),
                font=SANS,
                size=32,
            ),
        ),
        damage=Damage(downsample=0.5, jpeg_quality=40, ink=20, speckle=1800),
        seed=100,
    ),
)
