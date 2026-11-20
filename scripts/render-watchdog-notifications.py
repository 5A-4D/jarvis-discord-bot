#!/usr/bin/env python3
"""Render original, deterministic HUD motion graphics; no image service required.

Build-time only: uses Pillow and DejaVu fonts. The watchdog sends the saved GIFs
and does not render frames or need Pillow at runtime.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


WIDTH, HEIGHT, SCALE = 960, 360, 2
FRAMES, DURATION_MS = 48, 80
ROOT = Path(__file__).resolve().parent.parent
FONTS = Path('/usr/share/fonts/truetype/dejavu')
NAVY = (7, 13, 22)
WHITE = (230, 242, 249)
MUTED = (124, 149, 168)
CYAN = (78, 231, 222)
AMBER = (246, 168, 79)
RED = (240, 102, 92)


def mix(a, b, value):
    return tuple(round(x + (y - x) * value) for x, y in zip(a, b))


class Canvas:
    def __init__(self, image):
        self.image = image
        self.draw = ImageDraw.Draw(image)

    def line(self, points, color, width=1):
        self.draw.line([(round(x * SCALE), round(y * SCALE)) for x, y in points],
                       fill=color, width=max(1, round(width * SCALE)))

    def rect(self, box, color, width=1, fill=None):
        self.draw.rectangle([round(p * SCALE) for p in box], outline=color,
                            width=max(1, round(width * SCALE)), fill=fill)

    def circle(self, x, y, radius, color, width=1, fill=None):
        self.draw.ellipse([round(p * SCALE) for p in (x-radius, y-radius, x+radius, y+radius)],
                          outline=color, width=max(1, round(width * SCALE)), fill=fill)

    def arc(self, radius, start, end, color, width=1, center=(181, 187)):
        x, y = center
        self.draw.arc([round(p * SCALE) for p in (x-radius, y-radius, x+radius, y+radius)],
                      start, end, fill=color, width=round(width * SCALE))

    def text(self, xy, value, size, color, *, bold=False, mono=False):
        font = 'DejaVuSansMono' if mono else 'DejaVuSans'
        font += '-Bold.ttf' if bold else '.ttf'
        self.draw.text((round(xy[0]*SCALE), round(xy[1]*SCALE)), value,
                       font=ImageFont.truetype(str(FONTS / font), round(size*SCALE)), fill=color)


def background(accent):
    image = Image.new('RGB', (WIDTH*SCALE, HEIGHT*SCALE), NAVY)
    c = Canvas(image)
    for y in range(HEIGHT):
        shade = round(5 * (1 - y/HEIGHT))
        c.line([(0, y), (WIDTH, y)], mix(NAVY, (20, 35, 49), shade / 10))
    # Quiet engineering grid and a soft glow behind the central instrument.
    for x in range(22, WIDTH, 28):
        c.line([(x, 45), (x, 315)], (15, 26, 38))
    for y in range(45, 316, 28):
        c.line([(23, y), (937, y)], (15, 26, 38))
    glow = Image.new('RGB', image.size)
    g = Canvas(glow)
    for radius in range(155, 0, -3):
        g.circle(181, 187, radius, mix((0, 0, 0), accent, .13 * (1-radius/160)), fill=
                 mix((0, 0, 0), accent, .13 * (1-radius/160)))
    image = Image.blend(image, glow.filter(ImageFilter.GaussianBlur(18*SCALE)), .25)
    c = Canvas(image)
    c.rect((14, 14, 945, 345), (34, 51, 67))
    for x, direction in ((14, 1), (945, -1)):
        for y, vertical in ((14, 1), (345, -1)):
            c.line([(x+22*direction, y), (x, y), (x, y+16*vertical)], accent, 2)
    c.text((33, 24), 'J.A.R.V.I.S.', 17, WHITE, bold=True, mono=True)
    c.text((732, 28), 'CONNECTION MONITOR', 11, MUTED, mono=True)
    c.line([(33, 58), (927, 58)], (39, 57, 72))
    c.line([(333, 80), (333, 294)], (39, 57, 72))
    c.line([(33, 307), (927, 307)], (39, 57, 72))
    c.text((34, 322), 'OMEN  /  DISCORD', 10, MUTED, mono=True)
    return image


def frame(state, index, base):
    image = base.copy()
    c = Canvas(image)
    online = state == 'online'
    accent = CYAN if online else AMBER
    phase = index / FRAMES
    angle = phase * 360
    pulse = .5 + .5*math.sin(2*math.pi*phase)
    x, y = 181, 187

    # Concentric mechanical arcs counter-rotate; all cycles join seamlessly.
    for radius in (111, 97, 82, 58):
        c.circle(x, y, radius, mix(NAVY, accent, .2), .7)
    for tick in range(60):
        theta = math.radians(tick*6)
        inner = 114 if tick % 5 else 111
        outer = 118 if tick % 5 else 121
        c.line([(x+inner*math.cos(theta), y+inner*math.sin(theta)),
                (x+outer*math.cos(theta), y+outer*math.sin(theta))],
               mix(NAVY, accent, .5 if tick % 5 else .85), 1)
    for start in (0, 120, 240):
        c.arc(103, start + angle, start + angle + (78 if online else 42), accent, 3)
        c.arc(89, start - angle + 20, start - angle + 63, mix(NAVY, accent, .6), 2)
    c.arc(72, -angle, 185-angle, mix(accent, WHITE, .25), 2)
    c.arc(67, angle+40, angle+135, mix(NAVY, accent, .35), 5)
    for offset in (-1, 1):
        c.line([(x+offset*126, y), (x+offset*139, y)], accent, 1)
        c.line([(x, y+offset*126), (x, y+offset*134)], accent, 1)

    # The luminous core has real radial motion rather than a zoomed still.
    for radius in range(41, 0, -1):
        intensity = ((1-radius/45)**1.5) * (.45 + .13*pulse)
        c.circle(x, y, radius, mix(NAVY, accent, intensity), fill=mix(NAVY, accent, intensity))
    if online:
        c.circle(x, y, 25, mix(accent, WHITE, .65), 2)
        c.line([(x-11, y+1), (x-3, y+9), (x+13, y-10)], WHITE, 4)
    else:
        c.circle(x, y, 25, RED, 2)
        c.line([(x-8, y-8), (x+8, y+8)], WHITE, 3)
        c.line([(x-8, y+8), (x+8, y-8)], WHITE, 3)
    c.text((120, 278), 'LINK VERIFIED' if online else 'LINK INTERRUPTED', 10, accent, mono=True)

    c.circle(373, 95, 4, accent, fill=mix(NAVY, accent, .6 + .4*pulse))
    c.text((387, 86), 'CONNECTION RESTORED' if online else 'CONNECTION INTERRUPTED',
           12, accent, bold=True, mono=True)
    c.text((365, 115), 'BACK ONLINE' if online else 'SIGNAL LOST', 53, WHITE, bold=True)
    c.text((369, 184), 'Ready when you are, sir.' if online else 'Temporarily out of reach.',
           21, MUTED)

    # Online heartbeat travels through the channel; offline stays a broken line.
    c.rect((369, 231, 916, 277), (33, 53, 68), fill=(8, 19, 28))
    c.text((382, 245), 'LINK', 11, accent, mono=True)
    points = []
    for px in range(433, 900):
        if online:
            distance = ((px - 433) / 467 - phase + .5) % 1 - .5
            height = 18*math.exp(-((distance/.016)**2)) - 8*math.exp(-(((distance+.03)/.014)**2))
        else:
            height = 2*math.sin(px*.09 + phase*math.tau) if px < 621 else 0
        points.append((px, 254-height))
    if online:
        c.line(points, accent, 1.5)
        c.text((688, 321), 'HEARTBEAT + DISCORD VERIFIED', 10, accent, mono=True)
    else:
        c.line([p for p in points if p[0] < 650], mix(NAVY, accent, .75), 1.5)
        c.line([p for p in points if p[0] > 682], mix(NAVY, accent, .4), 1.5)
        c.line([(656, 261), (673, 246)], RED, 2)
        c.text((690, 321), 'RECORDED EVENT / AUTO CHECK', 10, accent, mono=True)
    return image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def save_gif(frames, path):
    # Share one palette across all frames to avoid colour flicker.
    palette = frames[0].quantize(colors=192, method=Image.Quantize.MEDIANCUT)
    indexed = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in frames]
    indexed[0].save(path, save_all=True, append_images=indexed[1:], duration=DURATION_MS,
                    loop=0, optimize=True, disposal=1)
    with Image.open(path) as check:
        assert check.n_frames == FRAMES and check.info['loop'] == 0
        assert check.size == frames[0].size
        check.seek(FRAMES-1)
        check.load()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'assets')
    parser.add_argument('--preview-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.preview_dir.mkdir(parents=True, exist_ok=True)
    all_frames = {}
    for state, color in (('online', CYAN), ('offline', AMBER)):
        base = background(color)
        frames = [frame(state, index, base) for index in range(FRAMES)]
        all_frames[state] = frames
        target = args.output_dir / f'jarvis-{state}-hud.gif'
        save_gif(frames, target)
        frames[0].save(args.preview_dir / f'{state}-poster.png')
        print(f'{target}: {target.stat().st_size} bytes, {FRAMES} frames, {FRAMES*DURATION_MS} ms')
    combined = []
    for online, offline in zip(all_frames['online'], all_frames['offline']):
        preview = Image.new('RGB', (WIDTH, HEIGHT*2+12), NAVY)
        preview.paste(online, (0, 0))
        preview.paste(offline, (0, HEIGHT+12))
        combined.append(preview)
    save_gif(combined, args.preview_dir / 'notification-previews.gif')
    combined[0].save(args.preview_dir / 'notification-previews.png')


if __name__ == '__main__':
    main()
