"""Draw the Major Arcana favicon source set. Requires Pillow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
SIZE = 32
TRANSPARENT = (0, 0, 0, 0)

INK = "#111A33"
GOLD = "#DDB95F"
IVORY = "#FFF1C4"
BLUE = "#174A78"
CYAN = "#74C9DA"
VIOLET = "#49366F"
RED = "#873748"
ROSE = "#BE4F70"
ORANGE = "#E06A32"
GREEN = "#3F6B57"
GRAY = "#8390A6"

GLOBAL_PALETTE = {INK, GOLD, IVORY, BLUE, CYAN, VIOLET, RED, ROSE, ORANGE, GREEN, GRAY}


def rgba(color: str) -> tuple[int, int, int, int]:
    return tuple(bytes.fromhex(color.removeprefix("#"))) + (255,)


def rect(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], color: str) -> None:
    draw.rectangle(box, fill=rgba(color))


def poly(draw: ImageDraw.ImageDraw, points: list[tuple[int, int]], color: str) -> None:
    draw.polygon(points, fill=rgba(color))


def card(field: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGBA", (SIZE, SIZE), TRANSPARENT)
    draw = ImageDraw.Draw(image)
    poly(draw, [(6, 0), (25, 0), (27, 2), (27, 29), (25, 31), (6, 31), (4, 29), (4, 2)], INK)
    poly(draw, [(8, 2), (23, 2), (25, 4), (25, 27), (23, 29), (8, 29), (6, 27), (6, 4)], GOLD)
    rect(draw, (8, 4, 23, 27), field)
    return image, draw


def fool() -> Image.Image:
    image, d = card(BLUE)
    # Traveler, feathered cap, bundle and the last safe pixel of the cliff.
    rect(d, (19, 6, 20, 7), GOLD)
    rect(d, (18, 7, 21, 8), GOLD)
    rect(d, (13, 8, 16, 11), IVORY)
    rect(d, (11, 7, 13, 8), ROSE)
    rect(d, (10, 6, 11, 7), IVORY)
    poly(d, [(12, 12), (17, 12), (19, 19), (15, 21), (10, 18)], ORANGE)
    rect(d, (19, 10, 20, 22), GOLD)
    rect(d, (20, 10, 22, 13), RED)
    rect(d, (11, 19, 13, 24), IVORY)
    rect(d, (16, 20, 18, 24), IVORY)
    rect(d, (8, 25, 16, 26), GOLD)
    rect(d, (8, 27, 12, 27), GOLD)
    return image


def magician() -> Image.Image:
    image, d = card(RED)
    d.ellipse((9, 6, 15, 10), outline=rgba(IVORY), width=2)
    d.ellipse((16, 6, 22, 10), outline=rgba(IVORY), width=2)
    d.line((10, 22, 21, 11), fill=rgba(GOLD), width=2)
    rect(d, (19, 9, 22, 10), IVORY)
    rect(d, (9, 24, 22, 25), GOLD)
    rect(d, (10, 22, 11, 23), CYAN)
    rect(d, (14, 22, 15, 23), GREEN)
    rect(d, (18, 22, 19, 23), ORANGE)
    rect(d, (21, 22, 22, 23), IVORY)
    return image


def high_priestess() -> Image.Image:
    image, d = card(VIOLET)
    rect(d, (9, 7, 11, 24), IVORY)
    rect(d, (20, 7, 22, 24), GRAY)
    rect(d, (8, 6, 12, 8), GOLD)
    rect(d, (19, 6, 23, 8), GOLD)
    rect(d, (8, 23, 12, 25), GOLD)
    rect(d, (19, 23, 23, 25), GOLD)
    d.ellipse((12, 10, 20, 21), fill=rgba(IVORY))
    d.ellipse((15, 9, 21, 18), fill=rgba(VIOLET))
    rect(d, (15, 6, 16, 7), CYAN)
    return image


def empress() -> Image.Image:
    image, d = card(GREEN)
    poly(d, [(10, 11), (12, 7), (15, 11), (18, 7), (21, 11), (20, 14), (11, 14)], GOLD)
    rect(d, (12, 15, 19, 21), ROSE)
    poly(d, [(11, 17), (15, 24), (20, 17), (18, 15), (15, 18), (13, 15)], IVORY)
    d.line((9, 24, 13, 17), fill=rgba(GOLD), width=1)
    d.line((22, 24, 18, 17), fill=rgba(GOLD), width=1)
    rect(d, (8, 20, 10, 21), GOLD)
    rect(d, (21, 20, 23, 21), GOLD)
    return image


def emperor() -> Image.Image:
    image, d = card(RED)
    d.arc((8, 7, 15, 15), 80, 290, fill=rgba(IVORY), width=2)
    d.arc((16, 7, 23, 15), 250, 100, fill=rgba(IVORY), width=2)
    poly(d, [(11, 12), (13, 8), (16, 12), (19, 8), (21, 12), (20, 15), (12, 15)], GOLD)
    rect(d, (11, 16, 20, 23), ORANGE)
    rect(d, (9, 20, 11, 26), GOLD)
    rect(d, (20, 20, 22, 26), GOLD)
    rect(d, (13, 17, 18, 20), IVORY)
    return image


def hierophant() -> Image.Image:
    image, d = card(VIOLET)
    rect(d, (15, 6, 16, 21), GOLD)
    rect(d, (11, 9, 20, 10), GOLD)
    rect(d, (12, 13, 19, 14), GOLD)
    rect(d, (14, 17, 17, 18), GOLD)
    d.ellipse((9, 20, 13, 24), outline=rgba(IVORY), width=2)
    d.ellipse((18, 20, 22, 24), outline=rgba(IVORY), width=2)
    d.line((12, 23, 19, 27), fill=rgba(IVORY), width=2)
    d.line((20, 23, 13, 27), fill=rgba(IVORY), width=2)
    return image


def lovers() -> Image.Image:
    image, d = card(ROSE)
    rect(d, (15, 6, 16, 7), GOLD)
    rect(d, (14, 7, 17, 8), GOLD)
    d.ellipse((9, 10, 13, 14), fill=rgba(IVORY))
    d.ellipse((18, 10, 22, 14), fill=rgba(IVORY))
    poly(d, [(8, 16), (13, 15), (15, 23), (10, 24)], BLUE)
    poly(d, [(23, 16), (18, 15), (16, 23), (21, 24)], VIOLET)
    poly(d, [(13, 17), (15, 16), (16, 18), (18, 16), (20, 17), (16, 23)], GOLD)
    return image


def chariot() -> Image.Image:
    image, d = card(BLUE)
    poly(d, [(9, 10), (12, 6), (19, 6), (22, 10)], GOLD)
    rect(d, (10, 10, 21, 20), IVORY)
    rect(d, (12, 12, 19, 18), RED)
    poly(d, [(16, 12), (18, 15), (16, 18), (14, 15)], GOLD)
    rect(d, (8, 20, 23, 22), GOLD)
    d.ellipse((9, 21, 14, 26), fill=rgba(INK))
    d.ellipse((18, 21, 23, 26), fill=rgba(INK))
    rect(d, (11, 23, 12, 24), IVORY)
    rect(d, (20, 23, 21, 24), IVORY)
    return image


def strength() -> Image.Image:
    image, d = card(ORANGE)
    d.ellipse((9, 5, 15, 9), outline=rgba(IVORY), width=2)
    d.ellipse((16, 5, 22, 9), outline=rgba(IVORY), width=2)
    poly(d, [(11, 13), (9, 10), (14, 11), (16, 9), (18, 11), (23, 10), (21, 13)], GOLD)
    d.ellipse((10, 12, 22, 25), fill=rgba(GOLD))
    d.ellipse((13, 14, 19, 22), fill=rgba(RED))
    rect(d, (12, 15, 13, 16), INK)
    rect(d, (19, 15, 20, 16), INK)
    rect(d, (15, 19, 17, 20), IVORY)
    return image


def hermit() -> Image.Image:
    image, d = card(BLUE)
    rect(d, (13, 6, 18, 7), GOLD)
    rect(d, (12, 8, 13, 11), GOLD)
    rect(d, (18, 8, 19, 11), GOLD)
    rect(d, (13, 11, 18, 12), GOLD)
    rect(d, (11, 13, 20, 14), GOLD)
    rect(d, (11, 15, 20, 24), GOLD)
    rect(d, (13, 17, 18, 22), ORANGE)
    rect(d, (14, 18, 17, 21), IVORY)
    rect(d, (10, 24, 21, 25), GOLD)
    return image


def wheel_of_fortune() -> Image.Image:
    image, d = card(VIOLET)
    d.ellipse((9, 7, 22, 24), outline=rgba(GOLD), width=2)
    d.ellipse((13, 12, 18, 19), fill=rgba(IVORY))
    d.line((16, 8, 16, 23), fill=rgba(GOLD), width=1)
    d.line((10, 16, 21, 16), fill=rgba(GOLD), width=1)
    d.line((11, 10, 21, 22), fill=rgba(GOLD), width=1)
    d.line((21, 10, 11, 22), fill=rgba(GOLD), width=1)
    rect(d, (15, 15, 17, 17), RED)
    return image


def justice() -> Image.Image:
    image, d = card(RED)
    rect(d, (15, 6, 16, 25), IVORY)
    poly(d, [(13, 8), (18, 8), (16, 5)], GOLD)
    rect(d, (9, 11, 22, 12), GOLD)
    d.line((11, 12, 9, 19), fill=rgba(GOLD), width=1)
    d.line((20, 12, 22, 19), fill=rgba(GOLD), width=1)
    poly(d, [(8, 19), (13, 19), (11, 22)], IVORY)
    poly(d, [(19, 19), (24, 19), (21, 22)], IVORY)
    poly(d, [(13, 25), (18, 25), (16, 28)], GOLD)
    return image


def hanged_man() -> Image.Image:
    image, d = card(GREEN)
    rect(d, (9, 6, 22, 7), GOLD)
    rect(d, (10, 7, 11, 25), GOLD)
    rect(d, (20, 7, 21, 25), GOLD)
    rect(d, (15, 7, 16, 12), IVORY)
    d.ellipse((13, 20, 18, 25), fill=rgba(IVORY))
    rect(d, (13, 13, 18, 20), BLUE)
    d.line((15, 13, 12, 10), fill=rgba(ORANGE), width=2)
    d.line((16, 13, 19, 10), fill=rgba(ORANGE), width=2)
    d.line((15, 12, 19, 8), fill=rgba(IVORY), width=2)
    return image


def death() -> Image.Image:
    image, d = card(INK)
    d.ellipse((10, 8, 19, 18), fill=rgba(IVORY))
    rect(d, (12, 16, 17, 21), IVORY)
    rect(d, (12, 12, 13, 13), INK)
    rect(d, (17, 12, 18, 13), INK)
    rect(d, (14, 15, 15, 16), INK)
    rect(d, (13, 18, 13, 20), INK)
    rect(d, (16, 18, 16, 20), INK)
    d.line((21, 7, 11, 26), fill=rgba(GRAY), width=2)
    d.arc((15, 5, 24, 13), 260, 80, fill=rgba(GOLD), width=2)
    return image


def temperance() -> Image.Image:
    image, d = card(BLUE)
    poly(d, [(9, 8), (14, 9), (13, 15), (10, 14)], GOLD)
    poly(d, [(18, 19), (23, 20), (22, 26), (19, 25)], GOLD)
    d.line((12, 14, 20, 21), fill=rgba(CYAN), width=2)
    d.line((13, 14, 21, 21), fill=rgba(IVORY), width=1)
    rect(d, (9, 7, 14, 8), IVORY)
    rect(d, (18, 18, 23, 19), IVORY)
    rect(d, (10, 24, 13, 25), GREEN)
    rect(d, (19, 8, 22, 9), GOLD)
    return image


def devil() -> Image.Image:
    image, d = card(RED)
    poly(d, [(11, 11), (9, 6), (14, 9), (16, 7), (18, 9), (23, 6), (21, 11)], GOLD)
    d.ellipse((10, 10, 22, 24), fill=rgba(VIOLET))
    rect(d, (12, 14, 13, 15), ORANGE)
    rect(d, (19, 14, 20, 15), ORANGE)
    poly(d, [(14, 18), (16, 16), (18, 18), (16, 22)], IVORY)
    rect(d, (9, 23, 12, 26), GOLD)
    rect(d, (20, 23, 23, 26), GOLD)
    return image


def tower() -> Image.Image:
    image, d = card(RED)
    poly(d, [(19, 5), (13, 13), (17, 13), (12, 21), (22, 11), (18, 11)], GOLD)
    rect(d, (10, 13, 21, 25), GRAY)
    rect(d, (9, 11, 12, 14), IVORY)
    rect(d, (14, 11, 17, 14), IVORY)
    rect(d, (19, 11, 22, 14), IVORY)
    rect(d, (14, 20, 17, 25), INK)
    rect(d, (11, 16, 12, 18), ORANGE)
    rect(d, (19, 16, 20, 18), ORANGE)
    rect(d, (9, 25, 22, 26), GOLD)
    return image


def star() -> Image.Image:
    image, d = card(BLUE)
    rect(d, (15, 7, 16, 24), GOLD)
    rect(d, (9, 15, 22, 16), GOLD)
    rect(d, (12, 12, 13, 13), GOLD)
    rect(d, (18, 12, 19, 13), GOLD)
    rect(d, (12, 18, 13, 19), GOLD)
    rect(d, (18, 18, 19, 19), GOLD)
    rect(d, (14, 13, 17, 18), IVORY)
    rect(d, (13, 14, 18, 17), IVORY)
    return image


def moon() -> Image.Image:
    image, d = card(VIOLET)
    d.ellipse((9, 8, 22, 23), fill=rgba(IVORY))
    d.ellipse((13, 7, 23, 20), fill=rgba(VIOLET))
    rect(d, (19, 9, 20, 10), GOLD)
    rect(d, (20, 22, 21, 23), GOLD)
    return image


def sun() -> Image.Image:
    image, d = card(RED)
    rect(d, (15, 7, 16, 10), GOLD)
    rect(d, (15, 21, 16, 24), GOLD)
    rect(d, (8, 15, 11, 16), GOLD)
    rect(d, (20, 15, 23, 16), GOLD)
    rect(d, (11, 11, 12, 12), GOLD)
    rect(d, (19, 11, 20, 12), GOLD)
    rect(d, (11, 19, 12, 20), GOLD)
    rect(d, (19, 19, 20, 20), GOLD)
    d.ellipse((12, 12, 19, 19), fill=rgba(ORANGE))
    rect(d, (14, 14, 17, 17), IVORY)
    return image


def judgement() -> Image.Image:
    image, d = card(BLUE)
    poly(d, [(9, 14), (17, 10), (19, 13), (12, 18)], GOLD)
    rect(d, (8, 14, 11, 19), IVORY)
    d.line((18, 9, 21, 6), fill=rgba(IVORY), width=1)
    d.line((19, 12, 23, 12), fill=rgba(IVORY), width=1)
    d.line((18, 15, 22, 18), fill=rgba(IVORY), width=1)
    rect(d, (10, 23, 12, 26), ROSE)
    rect(d, (15, 22, 17, 26), ROSE)
    rect(d, (20, 23, 22, 26), ROSE)
    rect(d, (9, 21, 23, 22), GOLD)
    return image


def world() -> Image.Image:
    image, d = card(GREEN)
    d.ellipse((9, 6, 22, 25), outline=rgba(GOLD), width=2)
    rect(d, (8, 10, 10, 13), CYAN)
    rect(d, (21, 10, 23, 13), CYAN)
    rect(d, (8, 19, 10, 22), GOLD)
    rect(d, (21, 19, 23, 22), GOLD)
    d.ellipse((14, 9, 17, 12), fill=rgba(IVORY))
    poly(d, [(15, 12), (18, 17), (16, 23), (13, 17)], VIOLET)
    d.line((14, 15, 10, 18), fill=rgba(IVORY), width=1)
    d.line((17, 15, 21, 18), fill=rgba(IVORY), width=1)
    return image


@dataclass(frozen=True)
class Arcana:
    number: int
    roman: str
    slug: str
    title: str
    draw: Callable[[], Image.Image]


CARDS = [
    Arcana(0, "0", "the-fool", "THE FOOL", fool),
    Arcana(1, "I", "the-magician", "THE MAGICIAN", magician),
    Arcana(2, "II", "the-high-priestess", "THE HIGH PRIESTESS", high_priestess),
    Arcana(3, "III", "the-empress", "THE EMPRESS", empress),
    Arcana(4, "IV", "the-emperor", "THE EMPEROR", emperor),
    Arcana(5, "V", "the-hierophant", "THE HIEROPHANT", hierophant),
    Arcana(6, "VI", "the-lovers", "THE LOVERS", lovers),
    Arcana(7, "VII", "the-chariot", "THE CHARIOT", chariot),
    Arcana(8, "VIII", "strength", "STRENGTH", strength),
    Arcana(9, "IX", "the-hermit", "THE HERMIT", hermit),
    Arcana(10, "X", "wheel-of-fortune", "WHEEL OF FORTUNE", wheel_of_fortune),
    Arcana(11, "XI", "justice", "JUSTICE", justice),
    Arcana(12, "XII", "the-hanged-man", "THE HANGED MAN", hanged_man),
    Arcana(13, "XIII", "death", "DEATH", death),
    Arcana(14, "XIV", "temperance", "TEMPERANCE", temperance),
    Arcana(15, "XV", "the-devil", "THE DEVIL", devil),
    Arcana(16, "XVI", "the-tower", "THE TOWER", tower),
    Arcana(17, "XVII", "the-star", "THE STAR", star),
    Arcana(18, "XVIII", "the-moon", "THE MOON", moon),
    Arcana(19, "XIX", "the-sun", "THE SUN", sun),
    Arcana(20, "XX", "judgement", "JUDGEMENT", judgement),
    Arcana(21, "XXI", "the-world", "THE WORLD", world),
]


def validate(image: Image.Image, name: str) -> None:
    pixels = set(image.get_flattened_data())
    allowed = {rgba(color) for color in GLOBAL_PALETTE}
    if image.mode != "RGBA" or image.size != (SIZE, SIZE):
        raise ValueError(f"{name}: expected a 32x32 RGBA image")
    if {pixel[3] for pixel in pixels} - {0, 255}:
        raise ValueError(f"{name}: partial alpha is not allowed")
    if {pixel for pixel in pixels if pixel[3]} - allowed:
        raise ValueError(f"{name}: contains an undeclared color")


def load_font(size: int) -> ImageFont.ImageFont:
    for path in (Path("C:/Windows/Fonts/consola.ttf"), Path("C:/Windows/Fonts/arial.ttf")):
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def contact_sheet(rendered: list[tuple[Arcana, Image.Image]], source_size: int, scale: int) -> Image.Image:
    columns = 5
    rows = (len(rendered) + columns - 1) // columns
    icon_size = source_size * scale
    tile_width = 210
    tile_height = icon_size + 42
    sheet = Image.new("RGB", (columns * tile_width, rows * tile_height), (235, 239, 245))
    draw = ImageDraw.Draw(sheet)
    font = load_font(14)
    label_color = (24, 34, 55)

    for index, (arcana, image) in enumerate(rendered):
        column = index % columns
        row = index // columns
        left = column * tile_width
        top = row * tile_height
        source = image if source_size == 32 else image.resize((16, 16), Image.Resampling.NEAREST)
        icon = source.resize((icon_size, icon_size), Image.Resampling.NEAREST)
        sheet.paste(icon, (left + (tile_width - icon_size) // 2, top), icon)
        label = f"{arcana.number:02d} {arcana.roman} {arcana.title}"
        bounds = draw.textbbox((0, 0), label, font=font)
        width = bounds[2] - bounds[0]
        draw.text((left + max(4, (tile_width - width) // 2), top + icon_size + 8), label, fill=label_color, font=font)
    return sheet


def main() -> None:
    rendered: list[tuple[Arcana, Image.Image]] = []
    for arcana in CARDS:
        image = arcana.draw()
        filename = f"{arcana.number:02d}-{arcana.slug}-32.png"
        validate(image, filename)
        image.save(ROOT / filename, optimize=True)
        rendered.append((arcana, image))
        print(ROOT / filename)

    contact_sheet(rendered, source_size=32, scale=4).save(ROOT / "major-arcana-32-contact-sheet.png", optimize=True)
    contact_sheet(rendered, source_size=16, scale=8).save(ROOT / "major-arcana-16-contact-sheet.png", optimize=True)
    print(ROOT / "major-arcana-32-contact-sheet.png")
    print(ROOT / "major-arcana-16-contact-sheet.png")


if __name__ == "__main__":
    main()
