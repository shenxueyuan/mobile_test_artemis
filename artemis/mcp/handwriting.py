# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Offline handwriting: turn text into pen strokes and inject them on a canvas.

A canvas that asks for handwriting (dictation, 默写, 描红) cannot be served by
``input_text``: the app wants a pen trajectory, not a string. Asking a vision
model to invent that trajectory does not work either -- CJK glyphs need stroke
order and curvature no model can emit point by point.

This module takes the third road: the trajectory comes from real stroke data
(the bundled ``strokes.db``), and the model only decides *what* to write and
*where*. Each stroke is injected as one continuous gesture, so the canvas sees
ordinary pen input.

Stroke data layout (one row per character in ``t_stroke``):

* ``median`` -- JSON list of polylines, one per stroke, in stroke order. These
  are the stroke centrelines: exactly the path a pen would follow.
* Coordinates are in a 1024x900 box with the **y axis pointing up** (glyph
  space), matching how the glyph is designed rather than how the screen is
  addressed.
"""

from __future__ import annotations

import json
import math
import sqlite3
from functools import lru_cache
from pathlib import Path

from artemis.config.paths import ROOT_DIR, get_app_dir
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "HandwritingError",
    "MissingStrokeDataError",
    "STROKE_SOURCES",
    "build_motion_scripts",
    "render_text",
    "stroke_db_path",
    "supported_characters",
]

#: Environment override for the stroke database location.
STROKE_DB_ENV = "ARTEMIS_STROKE_DB"
STROKE_DB_NAME = "strokes.db"

#: Glyph design box (y axis points up).
GLYPH_WIDTH = 1024.0
GLYPH_HEIGHT = 900.0

#: Points injected per stroke. The stored medians are sparse (3-8 points); the
#: pen needs a dense path, and the device interpolates between events anyway.
DEFAULT_POINTS_PER_STROKE = 22
#: Fraction of the canvas cell kept clear on each side.
DEFAULT_PADDING = 0.10
#: Pause between strokes so the app closes one stroke before the next begins.
DEFAULT_STROKE_GAP_MS = 120
#: Roughly the largest shell script to hand to one ``adb shell`` invocation.
_MAX_SCRIPT_CHARS = 4000

#: Accepted input sources. ``auto`` resolves to ``stylus`` (see the actuator):
#: pen-only canvases ignore finger events, while canvases that never ask for a
#: pen still receive stylus events as ordinary touches.
STROKE_SOURCES = ("auto", "stylus", "touchscreen")


class HandwritingError(Exception):
    """Raised when text cannot be turned into strokes."""


class MissingStrokeDataError(HandwritingError):
    """Raised when the stroke database simply has no entry for a character.

    Distinct from a database that cannot be read at all: the first is a normal
    "this character is not drawable", the second is a broken installation and
    must not be reported as a missing character.
    """


def stroke_db_path() -> Path | None:
    """Locate the stroke database, or ``None`` when it is not installed."""
    import os

    from artemis.resources import get_bundled_data_path

    override = os.environ.get(STROKE_DB_ENV)
    candidates: list[Path | None] = [
        Path(override) if override else None,
        ROOT_DIR / STROKE_DB_NAME,
        get_app_dir() / STROKE_DB_NAME,
        get_bundled_data_path(STROKE_DB_NAME),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=512)
def _medians_for(character: str) -> tuple[tuple[tuple[float, float], ...], ...]:
    """Stroke centrelines for one character, cached (the DB row is ~4 KB)."""
    path = stroke_db_path()
    if path is None:
        raise HandwritingError(
            f"Stroke database {STROKE_DB_NAME} not found. Place it next to the project"
            f" root or point {STROKE_DB_ENV} at it."
        )
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT median FROM t_stroke WHERE character = ? LIMIT 1", (character,)
            ).fetchone()
    except sqlite3.Error as exc:
        raise HandwritingError(f"Could not read {path}: {exc}") from exc
    if row is None or not row[0]:
        raise MissingStrokeDataError(f"No stroke data for '{character}'.")

    try:
        raw = json.loads(row[0])
    except (TypeError, ValueError) as exc:
        raise HandwritingError(f"Malformed stroke data for '{character}': {exc}") from exc

    strokes: list[tuple[tuple[float, float], ...]] = []
    for stroke in raw:
        points = tuple((float(p[0]), float(p[1])) for p in stroke if len(p) >= 2)
        if points:
            strokes.append(points)
    if not strokes:
        raise MissingStrokeDataError(f"Stroke data for '{character}' is empty.")
    return tuple(strokes)


def supported_characters(text: str) -> list[str]:
    """The characters in ``text`` that the stroke database can draw."""
    available = []
    for char in text:
        try:
            _medians_for(char)
        except MissingStrokeDataError:
            continue
        available.append(char)
    return available


def _smooth(points: tuple[tuple[float, float], ...], samples: int = 6) -> list[tuple[float, float]]:
    """Catmull-Rom through the stored centrelines (they are sparse by design)."""
    if len(points) < 3:
        return list(points)
    padded = [points[0], *points, points[-1]]
    out: list[tuple[float, float]] = []
    for i in range(len(padded) - 3):
        p0, p1, p2, p3 = padded[i], padded[i + 1], padded[i + 2], padded[i + 3]
        for s in range(samples):
            t = s / samples
            t2, t3 = t * t, t * t * t
            out.append(
                (
                    0.5
                    * (
                        2 * p1[0]
                        + (-p0[0] + p2[0]) * t
                        + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                        + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3
                    ),
                    0.5
                    * (
                        2 * p1[1]
                        + (-p0[1] + p2[1]) * t
                        + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                        + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
                    ),
                )
            )
    out.append(points[-1])
    return out


def _resample(points: list[tuple[float, float]], count: int) -> list[tuple[float, float]]:
    """Evenly resample a path to ``count`` points (a pen moves at a steady pace)."""
    if len(points) < 2:
        return list(points) * max(1, count)
    lengths = [0.0]
    for a, b in zip(points, points[1:]):
        lengths.append(lengths[-1] + math.dist(a, b))
    total = lengths[-1]
    if total <= 0:
        return [points[0]] * count
    out: list[tuple[float, float]] = []
    for i in range(count):
        target = total * i / max(1, count - 1)
        for j in range(1, len(lengths)):
            if lengths[j] >= target:
                span = lengths[j] - lengths[j - 1] or 1.0
                t = (target - lengths[j - 1]) / span
                a, b = points[j - 1], points[j]
                out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
                break
        else:
            out.append(points[-1])
    return out


def _cell_boxes(
    box: tuple[int, int, int, int], count: int, padding: float
) -> list[tuple[float, float, float, float]]:
    """Split a canvas box into ``count`` side-by-side cells (left to right)."""
    left, top, right, bottom = (float(v) for v in box)
    width = (right - left) / max(1, count)
    boxes = []
    for i in range(count):
        cell_left = left + width * i
        pad_x = padding * width
        pad_y = padding * (bottom - top)
        boxes.append(
            (
                cell_left + pad_x,
                top + pad_y,
                cell_left + width - pad_x,
                bottom - pad_y,
            )
        )
    return boxes


def _to_canvas(
    points: list[tuple[float, float]], cell: tuple[float, float, float, float]
) -> list[tuple[int, int]]:
    """Map glyph-space points into a canvas cell, keeping the glyph's aspect ratio."""
    left, top, right, bottom = cell
    cell_w, cell_h = right - left, bottom - top
    scale = min(cell_w / GLYPH_WIDTH, cell_h / GLYPH_HEIGHT)
    off_x = left + (cell_w - GLYPH_WIDTH * scale) / 2
    off_y = top + (cell_h - GLYPH_HEIGHT * scale) / 2
    # Glyph y points up; screen y points down.
    return [
        (round(off_x + gx * scale), round(off_y + (GLYPH_HEIGHT - gy) * scale)) for gx, gy in points
    ]


def render_text(
    text: str,
    box: tuple[int, int, int, int],
    *,
    points_per_stroke: int = DEFAULT_POINTS_PER_STROKE,
    padding: float = DEFAULT_PADDING,
) -> list[list[tuple[int, int]]]:
    """Render ``text`` as pixel-space strokes inside ``box`` (pixel bounds).

    Characters are laid out left to right, one cell each. Strokes keep their
    writing order, so a canvas that checks stroke order sees the real thing.
    """
    characters = [char for char in text if not char.isspace()]
    if not characters:
        raise HandwritingError("Nothing to write: the text is empty.")

    missing = [char for char in characters if char not in supported_characters(text)]
    if missing:
        raise HandwritingError(
            f"No stroke data for {' '.join(missing)}. Only characters present in"
            f" {STROKE_DB_NAME} can be hand-written."
        )

    strokes: list[list[tuple[int, int]]] = []
    for char, cell in zip(characters, _cell_boxes(box, len(characters), padding)):
        for median in _medians_for(char):
            dense = _resample(_smooth(median), max(2, points_per_stroke))
            strokes.append(_to_canvas(dense, cell))
    return strokes


def _motion_command(source: str, phase: str, x: int, y: int) -> str:
    prefix = "" if source == "touchscreen" else f"{source} "
    return f"input {prefix}motionevent {phase} {x} {y}"


def build_motion_scripts(
    strokes: list[list[tuple[int, int]]],
    *,
    source: str = "stylus",
    point_interval_ms: int = 0,
    stroke_gap_ms: int = DEFAULT_STROKE_GAP_MS,
    max_script_chars: int = _MAX_SCRIPT_CHARS,
) -> list[str]:
    """Build the ``adb shell`` scripts that draw ``strokes``.

    One gesture per stroke (DOWN, MOVE..., UP) and a pause between strokes, so
    the app commits each stroke before the next one starts. Long text is split
    across several scripts at stroke boundaries to stay within shell limits.
    """
    if source not in STROKE_SOURCES:
        raise HandwritingError(
            f"Unknown stroke source '{source}'. Use one of: {', '.join(STROKE_SOURCES)}."
        )
    resolved = "stylus" if source == "auto" else source

    scripts: list[str] = []
    current: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            scripts.append("; ".join(current))
            current, size = [], 0

    for stroke in strokes:
        if len(stroke) < 2:
            continue
        commands: list[str] = []
        for i, (x, y) in enumerate(stroke):
            phase = "DOWN" if i == 0 else "UP" if i == len(stroke) - 1 else "MOVE"
            commands.append(_motion_command(resolved, phase, int(x), int(y)))
            if point_interval_ms > 0 and 0 < i < len(stroke) - 1:
                commands.append(f"sleep {point_interval_ms / 1000:.3f}")
        commands.append(f"sleep {stroke_gap_ms / 1000:.3f}")

        cost = sum(len(c) + 2 for c in commands)
        if size + cost > max_script_chars:
            flush()
        current.extend(commands)
        size += cost
    flush()
    return scripts
