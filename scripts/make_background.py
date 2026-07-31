#!/usr/bin/env python3
"""
Render the Mandelbrot background used as the page's decorative backdrop.

This is a ONE-OFF authoring tool, not part of the site build: run it by hand and
commit the resulting image under assets/. The refresh workflow just copies that
file into the published output, so page loads never recompute the fractal and
the site build has no numpy/Pillow dependency.

    python3 scripts/make_background.py

Palette follows the classic escape-time look: near-black interior, a deep blue
outer field, and a magenta -> pink -> orange -> gold glow along the boundary.
"""

from __future__ import annotations

import os

import numpy as np
from PIL import Image

WIDTH, HEIGHT = 1920, 1080
# Deliberately modest: the escape-time gradient is very steep near the boundary,
# so a high cap squeezes the whole magenta->gold range into a near-invisible
# hairline. ~160 widens the glow into a band you can actually see, and at this
# zoom the boundary filaments are still fully resolved.
MAX_ITER = 160

# Classic full view: main cardioid + period-2 bulb + satellites, 16:9.
CENTER_X, CENTER_Y = -0.70, 0.0
VIEW_WIDTH = 3.10

OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets",
    "mandelbrot.jpg",
)

# position -> RGB. Position is normalised, tone-mapped escape time.
STOPS = [
    (0.00, (3, 6, 28)),       # near-black navy, far field
    (0.22, (10, 26, 96)),     # deep blue
    (0.42, (32, 34, 150)),    # blue / indigo
    (0.56, (104, 32, 168)),   # violet
    (0.68, (186, 38, 148)),   # magenta
    (0.78, (240, 78, 116)),   # pink
    (0.87, (252, 148, 62)),   # orange
    (0.94, (255, 198, 92)),   # amber
    (1.00, (255, 240, 176)),  # gold highlight
]


def build_lut(size: int = 1024) -> np.ndarray:
    """Linear-interpolate the colour stops into a lookup table."""
    xs = np.array([p for p, _ in STOPS])
    cols = np.array([c for _, c in STOPS], dtype=float)
    t = np.linspace(0.0, 1.0, size)
    lut = np.empty((size, 3))
    for channel in range(3):
        lut[:, channel] = np.interp(t, xs, cols[:, channel])
    return lut


def mandelbrot() -> tuple[np.ndarray, np.ndarray]:
    """Return (smooth escape counts, interior mask)."""
    aspect = HEIGHT / WIDTH
    half_w = VIEW_WIDTH / 2.0
    half_h = half_w * aspect
    xs = np.linspace(CENTER_X - half_w, CENTER_X + half_w, WIDTH)
    ys = np.linspace(CENTER_Y - half_h, CENTER_Y + half_h, HEIGHT)
    c = xs[np.newaxis, :] + 1j * ys[:, np.newaxis]

    z = np.zeros_like(c)
    escaped_at = np.zeros(c.shape, dtype=np.float64)
    alive = np.ones(c.shape, dtype=bool)
    # Escape radius well above 2 gives a smoother continuous iteration count.
    escape_r2 = 1 << 16

    for i in range(MAX_ITER):
        z[alive] = z[alive] * z[alive] + c[alive]
        mag2 = np.zeros(c.shape)
        mag2[alive] = (z[alive] * np.conj(z[alive])).real
        just_escaped = alive & (mag2 > escape_r2)
        if just_escaped.any():
            # Continuous ("smooth") iteration count kills the concentric banding.
            nu = i + 1 - np.log(np.log(np.sqrt(mag2[just_escaped]))) / np.log(2.0)
            escaped_at[just_escaped] = nu
            alive &= ~just_escaped
        if not alive.any():
            break

    return escaped_at, alive  # `alive` == points still in the set


def render() -> Image.Image:
    escaped_at, interior = mandelbrot()

    # Tone-map. Median escape count is ~4 against a cap of 160, so a linear (or
    # even power) ramp leaves the field flat blue with a hairline of hot colour.
    # A log curve expands that crowded low end and pushes the boundary band up
    # through magenta -> orange -> gold.
    norm = np.clip(np.log1p(escaped_at) / np.log1p(MAX_ITER), 0.0, 1.0)

    lut = build_lut()
    idx = np.clip((norm * (len(lut) - 1)).astype(np.int32), 0, len(lut) - 1)
    rgb = lut[idx]
    rgb[interior] = (0, 0, 0)  # the set itself stays black

    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), mode="RGB")


def main() -> None:
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    img = render()
    # JPEG, not PNG: this is a smooth photographic-style gradient, where PNG runs
    # to several MB while JPEG holds up fine at a fraction of the size. It sits
    # behind a heavy scrim, so compression artefacts are invisible in practice.
    img.save(OUT_PATH, "JPEG", quality=82, optimize=True, progressive=True)
    print(f"wrote {OUT_PATH} ({os.path.getsize(OUT_PATH) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
