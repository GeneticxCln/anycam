"""Full-face masks and face paint that follow a tracked face.

A mask is painted once on a template laid out in *face units*: 1.0 is the gap
between the eyes, the eyes sit at (-0.5, 0) and (0.5, 0) and the mouth centre at
(0, MOUTH_Y).  For every detected face the template is warped so those three
points land on the face's own eyes and mouth, which makes the mask scale, roll
and follow a turn of the head.  Masks may carry an animated layer (a scanning
line, blinking LED eyes) drawn on top of the cached base a dozen times a second.
"""
from __future__ import annotations

import math
from typing import Callable

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

PPU = 150                      # template pixels per face unit
X0, X1 = -1.7, 1.7             # template extent in face units
Y0, Y1 = -2.3, 2.7
MOUTH_Y = 1.15
ANIM_FPS = 12
Color = tuple[int, int, int, int]


def _c(hex_color: str, alpha: int = 255) -> Color:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


class Art:
    """A drawing surface in face units, supersampled, with cut-out holes."""

    def __init__(self, ss: int = 2) -> None:
        self.ss = ss
        self.scale = PPU * ss
        self.size = (int((X1 - X0) * self.scale), int((Y1 - Y0) * self.scale))
        self.img = Image.new("RGBA", self.size, (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)
        self.cut = Image.new("L", self.size, 0)
        self.cd = ImageDraw.Draw(self.cut)

    # coordinates ---------------------------------------------------------------
    def p(self, x: float, y: float) -> tuple[float, float]:
        return (x - X0) * self.scale, (y - Y0) * self.scale

    def u(self, units: float) -> float:
        return units * self.scale

    def box(self, cx, cy, rx, ry):
        return [*self.p(cx - rx, cy - ry), *self.p(cx + rx, cy + ry)]

    # shapes -------------------------------------------------------------------
    def ellipse(self, cx, cy, rx, ry, fill=None, outline=None, width=0.0):
        self.d.ellipse(self.box(cx, cy, rx, ry), fill=fill, outline=outline,
                       width=max(0, int(self.u(width))))

    def poly(self, pts, fill=None, outline=None, width=0.0):
        pix = [self.p(x, y) for x, y in pts]
        self.d.polygon(pix, fill=fill)
        if outline:
            self.d.line(pix + [pix[0]], fill=outline, width=max(1, int(self.u(width))),
                        joint="curve")

    def line(self, pts, fill, width=0.04):
        self.d.line([self.p(x, y) for x, y in pts], fill=fill,
                    width=max(1, int(self.u(width))), joint="curve")

    def rect(self, x0, y0, x1, y1, fill=None, outline=None, width=0.0, radius=0.0):
        box = [*self.p(x0, y0), *self.p(x1, y1)]
        if radius:
            self.d.rounded_rectangle(box, radius=self.u(radius), fill=fill, outline=outline,
                                     width=max(0, int(self.u(width))))
        else:
            self.d.rectangle(box, fill=fill, outline=outline, width=max(0, int(self.u(width))))

    def arc(self, cx, cy, rx, ry, start, end, fill, width=0.04):
        self.d.arc(self.box(cx, cy, rx, ry), start, end, fill=fill,
                   width=max(1, int(self.u(width))))

    def hole(self, cx, cy, rx, ry):
        """Let the real face show through here."""
        self.cd.ellipse(self.box(cx, cy, rx, ry), fill=255)

    def glow(self, painter: Callable[["Art"], None], radius: float = 0.08, strength: int = 2):
        """Paint ``painter`` on a scratch layer and add it as a blurred halo + sharp copy."""
        scratch = Art(self.ss)
        painter(scratch)
        halo = scratch.img.filter(ImageFilter.GaussianBlur(self.u(radius)))
        for _ in range(strength):
            self.img.alpha_composite(halo)
        self.img.alpha_composite(scratch.img)
        self.d = ImageDraw.Draw(self.img)

    def clip_to(self, mask: Image.Image) -> None:
        alpha = ImageChops.multiply(self.img.getchannel("A"), mask)
        self.img.putalpha(alpha)

    def face_mask(self, cx=0.0, cy=0.4, rx=1.08, ry=1.55, feather=0.05) -> Image.Image:
        mask = Image.new("L", self.size, 0)
        ImageDraw.Draw(mask).ellipse(self.box(cx, cy, rx, ry), fill=255)
        return mask.filter(ImageFilter.GaussianBlur(self.u(feather)))

    def finish(self) -> Image.Image:
        cut = self.cut.filter(ImageFilter.GaussianBlur(self.u(0.025)))
        alpha = ImageChops.multiply(self.img.getchannel("A"), ImageChops.invert(cut))
        self.img.putalpha(alpha)
        return self.img.resize((self.size[0] // self.ss, self.size[1] // self.ss), Image.LANCZOS)


def _plain_overlay() -> Art:
    return Art(1)


# --- shared pieces ------------------------------------------------------------------

def _eye_holes(a: Art, rx=0.30, ry=0.2):
    for sx in (-0.5, 0.5):
        a.hole(sx, 0.0, rx, ry)


def _paint_face(a: Art, colour: Color, feather=0.08, cy=0.4, rx=1.06, ry=1.55):
    layer = Image.new("RGBA", a.size, colour)
    a.clip_to(a.face_mask(0, cy, rx, ry, feather))     # nothing yet: make area mask
    shape = a.face_mask(0, cy, rx, ry, feather)
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), shape))
    a.img.alpha_composite(layer)
    a.d = ImageDraw.Draw(a.img)


def _shade(a: Art, shape: Image.Image, colour: Color, offset=(0.0, 0.0)):
    layer = Image.new("RGBA", a.size, colour)
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), shape))
    a.img.alpha_composite(layer)
    a.d = ImageDraw.Draw(a.img)


# --- the masks -------------------------------------------------------------------------

def paint_cyber(a: Art):
    cyan, mag, dark = _c("#00f0ff"), _c("#ff2bd6"), _c("#071226", 235)
    # armoured plate over the upper face, eyes inside a visor
    a.poly([(-1.05, -0.55), (-0.2, -0.85), (0.2, -0.85), (1.05, -0.55), (1.15, 0.5),
            (0.75, 0.62), (0.4, 0.45), (0.0, 0.62), (-0.4, 0.45), (-0.75, 0.62), (-1.15, 0.5)],
           fill=dark)
    a.poly([(-1.05, 0.9), (-0.65, 0.78), (-0.35, 1.7), (0.0, 1.95), (0.35, 1.7), (0.65, 0.78),
            (1.05, 0.9), (0.95, 1.5), (0.45, 2.15), (-0.45, 2.15), (-0.95, 1.5)],
           fill=_c("#071226", 150))
    a.hole(-0.5, 0.02, 0.27, 0.13)
    a.hole(0.5, 0.02, 0.27, 0.13)

    def circuits(g: Art):
        for sx in (-1, 1):
            g.line([(sx * 0.2, -0.82), (sx * 0.2, -0.45), (sx * 0.34, -0.3), (sx * 0.9, -0.3)],
                   cyan, 0.022)
            g.line([(sx * 0.55, -0.7), (sx * 0.55, -0.45)], mag, 0.022)
            g.line([(sx * 1.1, 0.15), (sx * 1.1, 0.55), (sx * 0.85, 0.75)], cyan, 0.022)
            g.line([(sx * 0.75, 0.88), (sx * 0.55, 1.4), (sx * 0.3, 1.8)], mag, 0.02)
            g.line([(sx * 0.95, 1.0), (sx * 0.8, 1.45), (sx * 0.5, 1.95)], cyan, 0.02)
            g.ellipse(sx * 0.9, -0.3, 0.035, 0.035, fill=cyan)
            g.ellipse(sx * 0.55, -0.45, 0.03, 0.03, fill=mag)
        g.line([(-0.2, 0.7), (0, 0.88), (0.2, 0.7)], cyan, 0.02)
        g.rect(-0.42, 1.28, 0.42, 1.36, outline=cyan, width=0.015)
        for i in range(-3, 4):
            g.line([(i * 0.12, 1.28), (i * 0.12, 1.36)], cyan, 0.012)
        for sx in (-1, 1):                                  # visor rims
            g.arc(sx * 0.5, 0.02, 0.3, 0.16, 0, 360, cyan, 0.03)
    a.glow(circuits, 0.05, 2)


def anim_cyber(t: float) -> Image.Image:
    a = _plain_overlay()
    y = -0.8 + ((t * 0.7) % 1.0) * 3.0                    # scan line sweeping the face
    scan = a
    scan.line([(-1.2, y), (1.2, y)], _c("#aaffff", 255), 0.03)
    halo = scan.img.filter(ImageFilter.GaussianBlur(scan.u(0.06)))
    halo.alpha_composite(scan.img)
    pulse = 0.5 + 0.5 * math.sin(t * 5)
    eyes = Art(1)
    for sx in (-0.5, 0.5):
        eyes.ellipse(sx, 0.02, 0.2 + 0.05 * pulse, 0.07 + 0.02 * pulse,
                     fill=(0, 240, 255, int(110 + 100 * pulse)))
    glow = eyes.img.filter(ImageFilter.GaussianBlur(eyes.u(0.05)))
    halo.alpha_composite(glow)
    halo.alpha_composite(eyes.img)
    return halo


def paint_web_hero(a: Art):
    red, dark, white = _c("#d1202f"), _c("#0f2a6b"), _c("#ffffff")
    _paint_face(a, red, 0.06, 0.45, 1.1, 1.6)
    # blue side panels like a two-tone hood
    shape = a.face_mask(0, 0.45, 1.1, 1.6, 0.06)
    side = Image.new("L", a.size, 0)
    sd = ImageDraw.Draw(side)
    for sx in (-1, 1):
        sd.polygon([a.p(sx * 0.75, 0.9), a.p(sx * 1.2, 0.5), a.p(sx * 1.2, 2.2), a.p(sx * 0.7, 2.1)],
                   fill=255)
    _shade(a, ImageChops.multiply(shape, side), dark)
    # web lines
    black = _c("#161616")
    for ang in range(-80, 81, 20):
        r = 1.7
        a.line([(0.0, 0.2), (r * math.sin(math.radians(ang)), 0.2 + r * math.cos(math.radians(ang)) * 0.9)],
               black, 0.018)
        a.line([(0.0, 0.2), (r * math.sin(math.radians(ang)), 0.2 - r * math.cos(math.radians(ang)) * 0.8)],
               black, 0.018)
    for k in range(1, 7):
        r = k * 0.3
        pts = [(math.sin(math.radians(ang)) * r * 1.15,
                0.2 + math.cos(math.radians(ang)) * r * 0.85 - 0.1 * math.sin(abs(ang) * 0.06))
               for ang in range(-90, 91, 20)]
        a.line(pts, black, 0.014)
        a.line([(x, 0.4 - (y - 0.2) * 1.0) for x, y in pts], black, 0.014)
    # big angular lenses
    for sx in (-1, 1):
        lens = [(sx * 0.12, -0.25), (sx * 0.82, -0.42), (sx * 0.95, 0.2), (sx * 0.62, 0.32),
                (sx * 0.2, 0.18)]
        a.poly(lens, fill=black)
        inner = [(sx * 0.17, -0.2), (sx * 0.78, -0.36), (sx * 0.9, 0.16), (sx * 0.62, 0.27),
                 (sx * 0.24, 0.14)]
        a.poly(inner, fill=white)
        a.poly([(sx * 0.2, -0.17), (sx * 0.6, -0.27), (sx * 0.42, -0.12)], fill=_c("#dbe9ff"))
    a.clip_to(a.face_mask(0, 0.45, 1.1, 1.6, 0.06))


def paint_robot(a: Art):
    steel, dark, light = _c("#9aa6b2"), _c("#4a5560"), _c("#dfe6ec")
    shape = a.face_mask(0, 0.4, 1.12, 1.62, 0.02)
    _shade(a, shape, steel)
    # panels and rivets
    a.rect(-0.95, -0.7, 0.95, 0.55, outline=dark, width=0.03, radius=0.12)
    a.line([(0, -0.7), (0, -0.4)], dark, 0.03)
    a.line([(-1.0, 0.75), (1.0, 0.75)], dark, 0.025)
    for x, y in ((-0.9, -0.62), (0.9, -0.62), (-0.9, 0.48), (0.9, 0.48), (-0.9, 0.9), (0.9, 0.9)):
        a.ellipse(x, y, 0.045, 0.045, fill=light, outline=dark, width=0.012)
    # eyes: dark sockets with red lens
    for sx in (-0.5, 0.5):
        a.ellipse(sx, 0.0, 0.3, 0.22, fill=_c("#1b1f24"), outline=dark, width=0.03)
        a.ellipse(sx, 0.0, 0.17, 0.17, fill=_c("#ff2a2a"), outline=_c("#ffb3b3"), width=0.012)
        a.ellipse(sx - 0.05, -0.05, 0.05, 0.05, fill=(255, 255, 255, 200))
    a.poly([(-0.1, 0.45), (0.1, 0.45), (0.14, 0.85), (-0.14, 0.85)], fill=dark)
    # grille mouth
    a.rect(-0.55, 1.0, 0.55, 1.5, fill=_c("#1b1f24"), outline=dark, width=0.03, radius=0.06)
    for i in range(-4, 5):
        a.line([(i * 0.12, 1.02), (i * 0.12, 1.48)], steel, 0.035)
    # chin plate
    a.poly([(-0.7, 1.6), (0.7, 1.6), (0.5, 1.95), (-0.5, 1.95)], fill=_c("#7c8895"), outline=dark,
           width=0.025)


def anim_robot(t: float) -> Image.Image:
    a = _plain_overlay()
    pulse = 0.5 + 0.5 * math.sin(t * 6)
    for sx in (-0.5, 0.5):
        a.ellipse(sx, 0.0, 0.21, 0.21, fill=(255, 60, 60, int(60 + 120 * pulse)))
    glow = a.img.filter(ImageFilter.GaussianBlur(a.u(0.07)))
    glow.alpha_composite(a.img)
    return glow


def paint_skull(a: Art):
    bone, shade = _c("#f4efe1"), _c("#cfc7b0")
    _paint_face(a, bone, 0.05, 0.35, 1.05, 1.55)
    for sx in (-0.5, 0.5):
        a.ellipse(sx, 0.02, 0.36, 0.34, fill=_c("#10101a"))
    a.poly([(0, 0.55), (-0.17, 0.95), (0.17, 0.95)], fill=_c("#10101a"))
    a.poly([(-0.6, 1.1), (0.6, 1.1), (0.55, 1.9), (-0.55, 1.9)], fill=_c("#f4efe1"),
           outline=_c("#2a2a2a"), width=0.03)
    for i in range(-3, 4):
        a.line([(i * 0.15, 1.1), (i * 0.15, 1.9)], _c("#2a2a2a"), 0.03)
    a.line([(-0.58, 1.5), (0.58, 1.5)], _c("#2a2a2a"), 0.03)
    for sx in (-1, 1):
        a.line([(sx * 0.95, 0.4), (sx * 0.78, 0.9), (sx * 0.85, 1.2)], shade, 0.03)
    a.line([(0.15, -0.9), (0.05, -0.55), (0.18, -0.35)], _c("#a39a82"), 0.025)


def paint_sugar_skull(a: Art):
    _paint_face(a, _c("#fbf6ea"), 0.05, 0.4, 1.06, 1.6)
    colours = [_c("#e63a7a"), _c("#26a8e0"), _c("#ffb400"), _c("#7a3bd1")]
    for sx in (-0.5, 0.5):
        a.ellipse(sx, 0.0, 0.44, 0.4, fill=_c("#12121a"))
        a.ellipse(sx, 0.0, 0.5, 0.46, outline=colours[1], width=0.04)
        for k in range(10):                               # petals around the eye
            ang = k / 10 * math.tau
            a.ellipse(sx + math.cos(ang) * 0.6, math.sin(ang) * 0.54, 0.07, 0.07,
                      fill=colours[k % 4])
    a.poly([(0, 0.55), (-0.15, 0.9), (0.15, 0.9)], fill=_c("#12121a"))
    a.line([(-0.6, 1.25), (0.6, 1.25)], _c("#12121a"), 0.035)
    for i in range(-3, 4):
        a.line([(i * 0.16, 1.1), (i * 0.16, 1.5)], _c("#12121a"), 0.03)
    a.ellipse(0, -0.65, 0.22, 0.17, fill=colours[0], outline=_c("#12121a"), width=0.02)
    for sx in (-1, 1):                                    # flowers on the forehead
        a.ellipse(sx * 0.65, -0.55, 0.14, 0.14, fill=colours[2])
        a.ellipse(sx * 0.65, -0.55, 0.06, 0.06, fill=colours[0])


def paint_zombie(a: Art):
    _paint_face(a, _c("#7fae5b", 190), 0.07, 0.4, 1.06, 1.6)
    shape = a.face_mask(0, 0.4, 1.06, 1.6, 0.07)
    blotch = Image.new("L", a.size, 0)
    bd = ImageDraw.Draw(blotch)
    for x, y, r in ((-0.6, 0.6, 0.28), (0.7, 1.2, 0.22), (0.2, -0.65, 0.2), (-0.4, 1.5, 0.18)):
        bd.ellipse(a.box(x, y, r, r), fill=170)
    _shade(a, ImageChops.multiply(blotch.filter(ImageFilter.GaussianBlur(a.u(0.08))), shape),
           _c("#4a6a2e", 200))
    a.ellipse(0.5, 0.0, 0.34, 0.3, fill=_c("#2a1a20", 235))           # sunken eye
    a.ellipse(0.5, 0.0, 0.17, 0.17, fill=_c("#f1ecc8"))
    a.ellipse(0.5, 0.0, 0.07, 0.07, fill=_c("#8a1a1a"))
    a.line([(-0.3, 0.9), (0.2, 1.1), (0.6, 0.95)], _c("#52252a"), 0.06)
    for i in range(7):                                                 # stitches
        x = -0.28 + i * 0.12
        a.line([(x, 0.8 + 0.06 * i), (x + 0.04, 1.2 - 0.04 * i * 0.5)], _c("#241a16"), 0.025)
    a.line([(-0.75, -0.4), (-0.45, -0.65)], _c("#c04a4a"), 0.05)


def paint_vampire(a: Art):
    _paint_face(a, _c("#ece3ee", 150), 0.07, 0.4, 1.06, 1.6)
    for sx in (-0.5, 0.5):
        a.ellipse(sx, 0.0, 0.3, 0.22, fill=(180, 20, 40, 0))
        a.ellipse(sx, 0.0, 0.13, 0.13, fill=_c("#d40f2c"), outline=_c("#6a0010"), width=0.02)
        a.ellipse(sx, 0.0, 0.03, 0.13, fill=_c("#1a0004"))
        a.line([(sx - 0.34, -0.3 - 0.03 * (1 if sx > 0 else -1)), (sx + 0.34, -0.45 + 0.1 * (1 if sx > 0 else -1))],
               _c("#1a1018"), 0.07)
    a.ellipse(0, 1.2, 0.5, 0.18, fill=_c("#6a0a1c"))
    for sx in (-0.2, 0.2):
        a.poly([(sx - 0.07, 1.15), (sx + 0.07, 1.15), (sx, 1.55)], fill=_c("#ffffff"),
               outline=_c("#c8c0c8"), width=0.012)
    a.ellipse(0.28, 1.5, 0.03, 0.09, fill=_c("#cf1030"))


def paint_clown(a: Art):
    red, blue = _c("#e0182d"), _c("#1f6fe0")
    _paint_face(a, _c("#fbfbfb", 235), 0.06, 0.4, 1.06, 1.6)
    for sx in (-0.5, 0.5):
        a.poly([(sx, -0.45), (sx + 0.3, 0.0), (sx, 0.5), (sx - 0.3, 0.0)], fill=blue)
        a.hole(sx, 0.0, 0.22, 0.14)
        a.poly([(sx, -0.8), (sx + 0.1, -0.45), (sx - 0.1, -0.45)], fill=_c("#f5c400"))
    a.ellipse(0, 0.7, 0.2, 0.2, fill=red, outline=_c("#8a0a15"), width=0.02)
    a.ellipse(-0.06, 0.64, 0.06, 0.06, fill=(255, 255, 255, 170))
    a.arc(0, 1.0, 0.8, 0.55, 20, 160, red, 0.18)
    for sx in (-1, 1):
        a.ellipse(sx * 0.85, 1.1, 0.16, 0.16, fill=red)


def paint_tiger(a: Art):
    _paint_face(a, _c("#f08a1c", 215), 0.07, 0.4, 1.08, 1.6)
    shape = a.face_mask(0, 0.4, 1.08, 1.6, 0.07)
    muzzle = Image.new("L", a.size, 0)
    ImageDraw.Draw(muzzle).ellipse(a.box(0, 1.2, 0.62, 0.62), fill=255)
    _shade(a, ImageChops.multiply(muzzle.filter(ImageFilter.GaussianBlur(a.u(0.03))), shape),
           _c("#fff4de", 230))
    ink = _c("#1d1208")
    for sx in (-1, 1):
        for k, (y, length) in enumerate(((-0.8, 0.3), (-0.55, 0.38), (-0.3, 0.3))):
            a.poly([(sx * 0.2, y), (sx * (0.2 + length), y - 0.06), (sx * 0.2, y + 0.1)], fill=ink)
        for k, y in enumerate((0.3, 0.6, 0.9, 1.2)):
            a.poly([(sx * 1.08, y), (sx * (0.78 - k * 0.02), y - 0.05), (sx * 0.8, y + 0.1)],
                   fill=ink)
        for y in (0.95, 1.15, 1.35):
            a.line([(sx * 0.3, y), (sx * 0.8, y + (y - 1.15) * 0.4)], _c("#ffffff", 200), 0.012)
    a.poly([(-0.17, 0.7), (0.17, 0.7), (0, 0.9)], fill=_c("#e0587a"), outline=ink, width=0.02)
    a.line([(0, 0.9), (0, 1.05)], ink, 0.03)
    a.arc(-0.12, 1.05, 0.12, 0.1, 0, 180, ink, 0.03)
    a.arc(0.12, 1.05, 0.12, 0.1, 0, 180, ink, 0.03)
    for sx in (-0.5, 0.5):
        a.hole(sx, 0.0, 0.24, 0.17)


def paint_panda(a: Art):
    _paint_face(a, _c("#fafafa", 215), 0.07, 0.4, 1.06, 1.6)
    ink = _c("#16161a")
    for sx in (-0.5, 0.5):
        a.ellipse(sx, 0.05, 0.34, 0.45, fill=ink)
        a.hole(sx, 0.02, 0.2, 0.14)
        a.ellipse(sx + (0.18 if sx < 0 else -0.18), -0.13, 0.04, 0.04, fill=(255, 255, 255, 220))
    a.ellipse(0, 0.7, 0.2, 0.13, fill=ink)
    a.line([(0, 0.8), (0, 1.0)], ink, 0.035)
    a.arc(-0.14, 1.0, 0.14, 0.12, 0, 180, ink, 0.035)
    a.arc(0.14, 1.0, 0.14, 0.12, 0, 180, ink, 0.035)
    for sx in (-1, 1):                                    # ears
        a.ellipse(sx * 0.85, -1.05, 0.34, 0.34, fill=ink)


def paint_puppy(a: Art):
    _paint_face(a, _c("#d9a066", 120), 0.08, 0.4, 1.06, 1.6)
    brown = _c("#6b3f1e")
    for sx in (-1, 1):                                    # floppy ears
        a.poly([(sx * 0.9, -0.55), (sx * 1.35, -0.2), (sx * 1.4, 0.9), (sx * 1.0, 0.9),
                (sx * 0.85, 0.2)], fill=brown, outline=_c("#3d2410"), width=0.03)
    a.ellipse(0, 0.82, 0.24, 0.17, fill=_c("#16120e"))
    a.ellipse(-0.07, 0.77, 0.07, 0.04, fill=(255, 255, 255, 170))
    a.line([(0, 0.95), (0, 1.15)], _c("#16120e"), 0.035)
    a.arc(-0.16, 1.1, 0.16, 0.12, 0, 180, _c("#16120e"), 0.035)
    a.arc(0.16, 1.1, 0.16, 0.12, 0, 180, _c("#16120e"), 0.035)
    a.ellipse(0, 1.45, 0.2, 0.3, fill=_c("#ef6c8d"), outline=_c("#a43a56"), width=0.025)
    a.line([(0, 1.28), (0, 1.62)], _c("#a43a56"), 0.025)


def paint_alien(a: Art):
    _paint_face(a, _c("#8ee07a", 205), 0.07, 0.4, 1.02, 1.6)
    shape = a.face_mask(0, 0.4, 1.02, 1.6, 0.07)
    shine = Image.new("L", a.size, 0)
    ImageDraw.Draw(shine).ellipse(a.box(-0.4, -0.3, 0.6, 0.7), fill=120)
    _shade(a, ImageChops.multiply(shine.filter(ImageFilter.GaussianBlur(a.u(0.2))), shape),
           _c("#d9ffc8", 180))
    for sx in (-1, 1):
        a.poly([(sx * 0.12, -0.2), (sx * 0.95, -0.45), (sx * 0.85, 0.35), (sx * 0.3, 0.3)],
               fill=_c("#08140a"))
        a.ellipse(sx * 0.5, -0.04, 0.1, 0.07, fill=(255, 255, 255, 110))
    a.ellipse(-0.08, 0.8, 0.03, 0.05, fill=_c("#3d8a30"))
    a.ellipse(0.08, 0.8, 0.03, 0.05, fill=_c("#3d8a30"))
    a.line([(-0.2, 1.3), (0.2, 1.3)], _c("#3d8a30"), 0.04)


def paint_domino(a: Art):
    """A classic hero eye-mask with a bold sweep."""
    blue, dark = _c("#1950c8"), _c("#0b2a73")
    a.poly([(-1.15, -0.25), (-0.65, -0.52), (-0.25, -0.34), (0, -0.24), (0.25, -0.34),
            (0.65, -0.52), (1.15, -0.25), (1.05, 0.28), (0.65, 0.42), (0.28, 0.3), (0, 0.18),
            (-0.28, 0.3), (-0.65, 0.42), (-1.05, 0.28)], fill=blue, outline=dark, width=0.04)
    a.line([(-1.0, -0.15), (-0.5, -0.3), (-0.2, -0.2)], _c("#6ea0ff", 200), 0.03)
    a.line([(1.0, -0.15), (0.5, -0.3), (0.2, -0.2)], _c("#6ea0ff", 200), 0.03)
    a.hole(-0.5, 0.0, 0.26, 0.17)
    a.hole(0.5, 0.0, 0.26, 0.17)
    a.poly([(-0.09, -0.95), (0.09, -0.95), (0.0, -0.7)], fill=_c("#f5c400"))


def paint_ninja(a: Art):
    cloth, dark = _c("#15171c"), _c("#0a0b0e")
    a.poly([(-1.2, 0.35), (-0.5, 0.28), (0.0, 0.34), (0.5, 0.28), (1.2, 0.35), (1.1, 2.05),
            (0.0, 2.35), (-1.1, 2.05)], fill=cloth, outline=dark, width=0.03)
    a.poly([(-1.2, -0.9), (1.2, -0.9), (1.25, -0.45), (-1.25, -0.45)], fill=_c("#c5192d"))
    a.line([(-1.2, -0.67), (1.2, -0.67)], _c("#7a0d1a"), 0.03)
    a.ellipse(0, -0.67, 0.2, 0.16, fill=_c("#e8e8e8"), outline=_c("#7a0d1a"), width=0.02)
    a.line([(1.2, -0.67), (1.65, -0.35)], _c("#c5192d"), 0.16)
    a.line([(1.2, -0.67), (1.6, -0.95)], _c("#c5192d"), 0.14)
    a.line([(-0.9, 0.9), (0.9, 0.9)], _c("#2a2d36"), 0.03)
    a.line([(0.0, 0.4), (0.0, 2.2)], _c("#2a2d36"), 0.02)


def paint_devil(a: Art):
    _paint_face(a, _c("#d62a2a", 170), 0.07, 0.4, 1.06, 1.6)
    dark = _c("#2a0808")
    for sx in (-1, 1):                                    # horns
        a.poly([(sx * 0.35, -1.1), (sx * 0.85, -1.05), (sx * 0.95, -2.0), (sx * 0.62, -1.4)],
               fill=_c("#2b0c0c"), outline=_c("#601010"), width=0.03)
        a.line([(sx * 0.4, -0.55), (sx * 0.95, -0.9)], dark, 0.085)
        a.poly([(sx * 0.1, -0.1), (sx * 0.8, -0.32), (sx * 0.3, 0.12)], fill=_c("#f5c400", 200))
        a.hole(sx * 0.5, 0.0, 0.2, 0.13)
    a.poly([(-0.2, 1.4), (0.2, 1.4), (0.12, 2.1), (0.0, 2.35), (-0.12, 2.1)], fill=_c("#1c0606"))
    a.arc(0, 1.0, 0.5, 0.3, 15, 165, dark, 0.05)


def paint_cyborg(a: Art):
    """Half the face is metal, with a glowing red eye."""
    steel, dark = _c("#aab3bd"), _c("#3a434d")
    half = Image.new("L", a.size, 0)
    ImageDraw.Draw(half).polygon([a.p(0.02, -1.2), a.p(1.4, -1.2), a.p(1.4, 2.4), a.p(0.02, 2.4)],
                                 fill=255)
    shape = ImageChops.multiply(half.filter(ImageFilter.GaussianBlur(a.u(0.02))),
                                a.face_mask(0, 0.4, 1.08, 1.62, 0.02))
    _shade(a, shape, steel)
    a.line([(0.02, -1.0), (0.02, 1.9)], dark, 0.04)
    a.line([(0.3, -0.7), (1.0, -0.7)], dark, 0.025)
    a.line([(0.5, 0.65), (1.0, 0.8), (0.9, 1.5)], dark, 0.025)
    a.ellipse(0.5, 0.0, 0.3, 0.24, fill=_c("#14181c"), outline=dark, width=0.03)
    a.ellipse(0.5, 0.0, 0.17, 0.17, fill=_c("#ff2a2a"), outline=_c("#ffc0c0"), width=0.015)
    for x, y in ((0.2, -0.55), (0.9, -0.45), (0.95, 1.1), (0.4, 1.75)):
        a.ellipse(x, y, 0.04, 0.04, fill=_c("#eef2f5"), outline=dark, width=0.012)
    a.rect(0.2, 1.1, 0.95, 1.4, outline=dark, width=0.03, radius=0.05)
    for i in range(5):
        a.line([(0.3 + i * 0.14, 1.12), (0.3 + i * 0.14, 1.38)], dark, 0.025)


def anim_cyborg(t: float) -> Image.Image:
    a = _plain_overlay()
    pulse = 0.5 + 0.5 * math.sin(t * 4)
    a.ellipse(0.5, 0.0, 0.2, 0.2, fill=(255, 60, 60, int(60 + 150 * pulse)))
    glow = a.img.filter(ImageFilter.GaussianBlur(a.u(0.08)))
    glow.alpha_composite(a.img)
    return glow


def paint_neon_lines(a: Art):
    """Glowing contour lines like a light-suit: eyes, brows, nose bridge, jaw."""
    def lines(g: Art):
        col = _c("#39f6ff")
        pink = _c("#ff3ccf")
        for sx in (-1, 1):
            g.arc(sx * 0.5, 0.0, 0.34, 0.2, 0, 360, col, 0.025)
            g.line([(sx * 0.2, -0.38), (sx * 0.55, -0.5), (sx * 0.95, -0.4)], pink, 0.025)
            g.line([(sx * 0.12, 0.35), (sx * 0.2, 0.75), (sx * 0.05, 0.85)], col, 0.022)
            g.arc(0, 0.5, 1.0, 1.55, 25, 155, pink, 0.025)
            g.line([(sx * 0.9, 0.2), (sx * 1.0, 0.8), (sx * 0.8, 1.5), (sx * 0.4, 1.95)], col, 0.022)
        g.arc(0, 1.3, 0.45, 0.22, 10, 170, pink, 0.025)
        g.line([(0, -0.9), (0, -0.55)], col, 0.022)
    a.glow(lines, 0.045, 3)


def anim_neon(t: float) -> Image.Image:
    a = _plain_overlay()
    y = -0.9 + ((t * 0.5) % 1.0) * 3.2
    a.line([(-1.2, y), (1.2, y)], (255, 255, 255, 210), 0.02)
    glow = a.img.filter(ImageFilter.GaussianBlur(a.u(0.05)))
    glow.alpha_composite(a.img)
    return glow


def paint_masquerade(a: Art):
    gold, dark = _c("#e8b73a"), _c("#7a5410")
    a.poly([(-1.35, -0.1), (-1.0, -0.5), (-0.4, -0.55), (0, -0.35), (0.4, -0.55), (1.0, -0.5),
            (1.35, -0.1), (1.0, 0.45), (0.5, 0.5), (0.25, 0.25), (0, 0.15), (-0.25, 0.25),
            (-0.5, 0.5), (-1.0, 0.45)], fill=gold, outline=dark, width=0.04)
    a.hole(-0.5, 0.0, 0.26, 0.17)
    a.hole(0.5, 0.0, 0.26, 0.17)
    for sx in (-1, 1):
        a.line([(sx * 1.2, -0.25), (sx * 0.75, -0.4), (sx * 0.3, -0.25)], _c("#fff2b8"), 0.03)
        a.line([(sx * 1.25, 0.0), (sx * 1.6, -0.35), (sx * 1.75, -0.9)], _c("#a24ad8"), 0.07)
        a.line([(sx * 1.2, -0.1), (sx * 1.55, -0.6), (sx * 1.55, -1.3)], _c("#3a8ee0"), 0.07)
        a.line([(sx * 1.1, -0.2), (sx * 1.3, -0.9), (sx * 1.2, -1.6)], _c("#e0488a"), 0.07)
        a.ellipse(sx * 0.9, 0.1, 0.07, 0.07, fill=_c("#c5192d"))
    a.ellipse(0, -0.05, 0.1, 0.1, fill=_c("#c5192d"), outline=dark, width=0.02)


def paint_pirate(a: Art):
    black, red = _c("#14141a"), _c("#c01f2e")
    a.line([(-1.2, -0.45), (1.2, -0.45)], black, 0.05)
    a.ellipse(-0.5, 0.0, 0.34, 0.28, fill=black, outline=_c("#32323c"), width=0.03)
    a.line([(-0.5, -0.12), (-0.8, -0.5)], black, 0.04)
    a.poly([(-1.25, -0.95), (1.25, -0.95), (1.3, -0.5), (-1.3, -0.5)], fill=red)
    for x in range(-5, 6):
        a.ellipse(x * 0.24, -0.72, 0.035, 0.035, fill=_c("#ffffff", 220))
    a.line([(1.2, -0.7), (1.7, -0.4)], red, 0.16)
    a.arc(0.0, 1.2, 0.55, 0.3, 20, 160, _c("#3a1a10"), 0.06)
    a.poly([(-0.6, 1.5), (0.6, 1.5), (0.35, 2.4), (0, 2.5), (-0.35, 2.4)], fill=_c("#2b1a12", 235))


def paint_harlequin(a: Art):
    _paint_face(a, _c("#ffffff", 120), 0.06, 0.4, 1.06, 1.6)
    colours = [_c("#d1202f"), _c("#16161a")]
    rows = ((-0.9, -0.3), (-0.3, 0.3), (0.3, 0.9), (0.9, 1.5), (1.5, 2.0))
    for r, (y0, y1) in enumerate(rows):
        for c in range(-3, 3):
            if (r + c) % 2:
                a.poly([(c * 0.4, (y0 + y1) / 2), (c * 0.4 + 0.2, y0), (c * 0.4 + 0.4, (y0 + y1) / 2),
                        (c * 0.4 + 0.2, y1)], fill=colours[(r // 2) % 2])
    shape = a.face_mask(0, 0.4, 1.06, 1.6, 0.06)
    a.clip_to(shape)
    for sx in (-0.5, 0.5):
        a.hole(sx, 0.0, 0.28, 0.2)
        a.arc(sx, 0.0, 0.32, 0.24, 0, 360, _c("#ffffff"), 0.04)
    a.ellipse(0, 0.75, 0.13, 0.13, fill=_c("#d1202f"))


def paint_fox(a: Art):
    _paint_face(a, _c("#f08a2c", 200), 0.07, 0.4, 1.06, 1.6)
    shape = a.face_mask(0, 0.4, 1.06, 1.6, 0.07)
    white = Image.new("L", a.size, 0)
    wd = ImageDraw.Draw(white)
    for sx in (-1, 1):
        wd.polygon([a.p(sx * 0.2, 0.55), a.p(sx * 1.1, 0.7), a.p(sx * 0.6, 1.95), a.p(0, 2.0)],
                   fill=255)
    _shade(a, ImageChops.multiply(white.filter(ImageFilter.GaussianBlur(a.u(0.02))), shape),
           _c("#fff6ea", 235))
    ink = _c("#24160e")
    for sx in (-1, 1):
        a.poly([(sx * 0.55, -0.9), (sx * 0.95, -0.85), (sx * 0.9, -1.9), (sx * 0.6, -1.3)],
               fill=_c("#f08a2c"), outline=ink, width=0.03)
        a.poly([(sx * 0.62, -0.95), (sx * 0.85, -0.9), (sx * 0.85, -1.55)], fill=ink)
        a.poly([(sx * 0.12, -0.25), (sx * 0.9, -0.35), (sx * 0.4, 0.12)], fill=ink, outline=None)
        a.hole(sx * 0.5, 0.0, 0.18, 0.11)
    a.ellipse(0, 0.85, 0.12, 0.09, fill=ink)
    a.line([(0, 0.94), (0, 1.1)], ink, 0.03)
    a.arc(-0.1, 1.05, 0.1, 0.09, 0, 180, ink, 0.03)
    a.arc(0.1, 1.05, 0.1, 0.09, 0, 180, ink, 0.03)


def paint_snowman_face(a: Art):
    """A frosty face: pale blue skin, a carrot nose and coal eyes and smile."""
    _paint_face(a, _c("#e6f4ff", 235), 0.08, 0.4, 1.06, 1.6)
    coal = _c("#16181c")
    for sx in (-0.5, 0.5):
        a.ellipse(sx, 0.0, 0.17, 0.17, fill=coal)
        a.ellipse(sx - 0.04, -0.05, 0.04, 0.04, fill=(255, 255, 255, 190))
    a.poly([(-0.09, 0.55), (0.09, 0.55), (0.0, 1.3)], fill=_c("#f07a1a"), outline=_c("#b44f0a"), width=0.02)
    for i in range(7):
        ang = math.radians(25 + i * 22)
        a.ellipse(math.cos(ang) * 0.55, 1.1 + math.sin(ang) * 0.4, 0.05, 0.05, fill=coal)
    for sx in (-1, 1):
        a.ellipse(sx * 0.75, 0.75, 0.2, 0.14, fill=(255, 130, 160, 110))


# key -> (label, group, base painter, optional animator(t) -> overlay)
MASKS: dict[str, tuple[str, str, Callable, Callable | None]] = {
    "cyber": ("Cyber face", "Sci-fi", paint_cyber, anim_cyber),
    "neon-lines": ("Neon contour", "Sci-fi", paint_neon_lines, anim_neon),
    "robot": ("Robot", "Sci-fi", paint_robot, anim_robot),
    "cyborg": ("Cyborg", "Sci-fi", paint_cyborg, anim_cyborg),
    "alien": ("Alien", "Sci-fi", paint_alien, None),
    "web-hero": ("Web hero", "Heroes", paint_web_hero, None),
    "domino": ("Hero eye mask", "Heroes", paint_domino, None),
    "ninja": ("Ninja", "Heroes", paint_ninja, None),
    "skull": ("Skull", "Spooky", paint_skull, None),
    "sugar-skull": ("Sugar skull", "Spooky", paint_sugar_skull, None),
    "zombie": ("Zombie", "Spooky", paint_zombie, None),
    "vampire": ("Vampire", "Spooky", paint_vampire, None),
    "devil": ("Devil", "Spooky", paint_devil, None),
    "clown": ("Clown", "Fun", paint_clown, None),
    "harlequin": ("Harlequin", "Fun", paint_harlequin, None),
    "pirate": ("Pirate", "Fun", paint_pirate, None),
    "masquerade": ("Masquerade", "Fun", paint_masquerade, None),
    "snowman": ("Frosty snowman", "Fun", paint_snowman_face, None),
    "tiger": ("Tiger", "Animals", paint_tiger, None),
    "panda": ("Panda", "Animals", paint_panda, None),
    "puppy": ("Puppy", "Animals", paint_puppy, None),
    "fox": ("Fox", "Animals", paint_fox, None),
}

GROUPS: tuple[str, ...] = tuple(dict.fromkeys(item[1] for item in MASKS.values()))

_base_cache: dict[str, Image.Image] = {}
_anim_cache: dict[tuple[str, int], Image.Image] = {}


def base(key: str) -> Image.Image:
    if key not in _base_cache:
        art = Art(2)
        MASKS[key][2](art)
        _base_cache[key] = art.finish()
    return _base_cache[key]


def template(key: str, t: float) -> Image.Image:
    """The mask picture at time ``t`` (cached; animated masks step ~12 times a second)."""
    animator = MASKS[key][3]
    if animator is None:
        return base(key)
    step = int(t * ANIM_FPS)
    cached = _anim_cache.get((key, step % 4096))
    if cached is None:
        picture = base(key).copy()
        overlay = animator(step / ANIM_FPS)
        # the overlay is painted unsupersampled at the same pixel size as the base
        if overlay.size != picture.size:
            overlay = overlay.resize(picture.size, Image.BILINEAR)
        picture.alpha_composite(overlay)
        if len(_anim_cache) > 600:
            _anim_cache.clear()
        _anim_cache[(key, step % 4096)] = picture
        cached = picture
    return cached


def next_template_time(key: str, t: float) -> float | None:
    """When the animated mask ``key`` steps next, if that frame is not cached.

    The frame thread pre-renders it on a worker (the overlays are blurred, ~12
    ms), so a new animation step never lands on a video frame.
    """
    if MASKS[key][3] is None:
        return None
    step = int(t * ANIM_FPS) + 1
    if (key, step % 4096) in _anim_cache:
        return None
    return step / ANIM_FPS


# --- placing it on a face -------------------------------------------------------------

def _affine(points_img, scale_px: float):
    """Coefficients mapping image pixels -> template pixels from three landmarks."""
    (lx, ly), (rx, ry), (mx, my) = points_img
    tpl = np.array([[(-0.5 - X0) * scale_px, (0.0 - Y0) * scale_px, 1.0],
                    [(0.5 - X0) * scale_px, (0.0 - Y0) * scale_px, 1.0],
                    [(0.0 - X0) * scale_px, (MOUTH_Y - Y0) * scale_px, 1.0]])
    src = np.array([[lx, ly, 1.0], [rx, ry, 1.0], [mx, my, 1.0]])
    try:
        # image -> template: solve src @ M = tpl[:, :2]
        m = np.linalg.solve(src, tpl[:, :2])
    except np.linalg.LinAlgError:
        return None
    return m                                              # shape (3, 2)


def place(canvas: Image.Image, key: str, t: float, left_eye, right_eye, mouth,
          opacity: float = 1.0) -> None:
    """Warp the mask onto one face and composite it onto ``canvas`` (RGBA)."""
    tpl = template(key, t)
    scale = tpl.width / (X1 - X0)
    m = _affine([left_eye, right_eye, mouth], scale)
    if m is None:
        return
    a_mat = np.array([[m[0, 0], m[1, 0], m[2, 0]], [m[0, 1], m[1, 1], m[2, 1]], [0.0, 0.0, 1.0]])
    try:
        inverse = np.linalg.inv(a_mat)                    # template -> image
    except np.linalg.LinAlgError:
        return
    corners = np.array([[0, 0, 1], [tpl.width, 0, 1], [0, tpl.height, 1],
                        [tpl.width, tpl.height, 1]], dtype=float).T
    mapped = inverse @ corners
    x0 = max(0, int(math.floor(mapped[0].min())))
    y0 = max(0, int(math.floor(mapped[1].min())))
    x1 = min(canvas.width, int(math.ceil(mapped[0].max())))
    y1 = min(canvas.height, int(math.ceil(mapped[1].max())))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return
    data = (a_mat[0, 0], a_mat[0, 1], a_mat[0, 0] * x0 + a_mat[0, 1] * y0 + a_mat[0, 2],
            a_mat[1, 0], a_mat[1, 1], a_mat[1, 0] * x0 + a_mat[1, 1] * y0 + a_mat[1, 2])
    warped = tpl.transform((x1 - x0, y1 - y0), Image.AFFINE, data, resample=Image.BILINEAR)
    if opacity < 1.0:
        warped.putalpha(warped.getchannel("A").point(lambda v: int(v * opacity)))
    canvas.alpha_composite(warped, (x0, y0))
