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

"""The handwriting engine: stroke data -> pen strokes -> one gesture per stroke.

A handwriting canvas wants a pen trajectory, so the trajectory is built from real
stroke data instead of being invented by a model. These tests pin the parts that
decide what the device receives: stroke order, the canvas mapping, the gesture
shape, and the input source.
"""

import sqlite3

import pytest

from artemis.mcp import handwriting
from artemis.mcp.handwriting import (
    HandwritingError,
    build_motion_scripts,
    render_text,
    stroke_db_path,
)

BOX = (100, 200, 500, 600)

requires_db = pytest.mark.skipif(stroke_db_path() is None, reason="strokes.db is not installed")


# --- Stroke data ---------------------------------------------------------------------


@requires_db
def test_render_keeps_stroke_order_and_stays_inside_the_box():
    strokes = render_text("毯", BOX)

    assert len(strokes) == 12
    # Density is proportional to each stroke's arc length: bounded between MIN
    # and MAX, and cheaper than the old fixed count on average.
    assert all(
        handwriting.MIN_POINTS_PER_STROKE <= len(stroke) <= handwriting.MAX_POINTS_PER_STROKE
        for stroke in strokes
    )
    assert sum(len(stroke) for stroke in strokes) < 12 * handwriting.MAX_POINTS_PER_STROKE
    for stroke in strokes:
        for x, y in stroke:
            assert BOX[0] <= x <= BOX[2], f"{x} outside the canvas box"
            assert BOX[1] <= y <= BOX[3], f"{y} outside the canvas box"


@requires_db
def test_multi_character_text_is_laid_out_left_to_right():
    strokes = render_text("苹果", BOX)

    # 苹 and 果 are eight strokes each; the first character owns the left cell.
    assert len(strokes) == 16
    left = [x for stroke in strokes[:8] for x, _ in stroke]
    right = [x for stroke in strokes[8:] for x, _ in stroke]
    assert max(left) <= min(right)


@requires_db
def test_unsupported_characters_are_reported_not_drawn():
    with pytest.raises(HandwritingError, match="a"):
        render_text("毯a", BOX)


def test_whitespace_only_text_is_rejected():
    with pytest.raises(HandwritingError, match="empty"):
        render_text("   ", BOX)


def test_a_broken_database_is_reported(tmp_path, monkeypatch):
    broken = tmp_path / "strokes.db"
    sqlite3.connect(broken).close()
    monkeypatch.setenv(handwriting.STROKE_DB_ENV, str(broken))
    handwriting._medians_for.cache_clear()
    try:
        assert stroke_db_path() == broken
        with pytest.raises(HandwritingError, match="Could not read"):
            render_text("毯", BOX)
    finally:
        handwriting._medians_for.cache_clear()


# --- Gesture scripts -----------------------------------------------------------------


def test_each_stroke_becomes_one_down_move_up_gesture():
    strokes = [[(1, 2), (3, 4), (5, 6)], [(7, 8), (9, 10)]]

    scripts = build_motion_scripts(strokes, source="stylus")

    assert len(scripts) == 1
    commands = scripts[0].split("; ")
    assert commands[0] == "input stylus motionevent DOWN 1 2"
    assert commands[1] == "input stylus motionevent MOVE 3 4"
    assert commands[2] == "input stylus motionevent UP 5 6"
    assert commands[3].startswith("sleep ")
    assert commands[4] == "input stylus motionevent DOWN 7 8"
    assert commands[5] == "input stylus motionevent UP 9 10"


def test_auto_source_injects_pen_events_and_touchscreen_omits_the_prefix():
    strokes = [[(1, 2), (3, 4)]]

    auto = build_motion_scripts(strokes, source="auto")[0]
    finger = build_motion_scripts(strokes, source="touchscreen")[0]

    assert auto.startswith("input stylus motionevent DOWN")
    assert finger.startswith("input motionevent DOWN")


def test_an_unknown_source_is_rejected():
    with pytest.raises(HandwritingError, match="Unknown stroke source"):
        build_motion_scripts([[(1, 2), (3, 4)]], source="laser")


def test_point_interval_adds_a_pause_between_points():
    strokes = [[(1, 2), (3, 4), (5, 6)]]

    paced = build_motion_scripts(strokes, source="stylus", point_interval_ms=40)[0]

    assert "sleep 0.040" in paced


def test_long_text_is_split_at_stroke_boundaries():
    strokes = [[(i, i), (i + 1, i + 1), (i + 2, i + 2)] for i in range(60)]

    scripts = build_motion_scripts(strokes, source="stylus", max_script_chars=200)

    assert len(scripts) > 1
    assert all(len(script) <= 400 for script in scripts)
    for script in scripts:
        # No script hands over a half-injected stroke: every one opens and closes
        # its gestures, so a chunk boundary never cuts a stroke in two.
        motion = [command for command in script.split("; ") if "motionevent" in command]
        assert " DOWN " in f"{motion[0]} "
        assert " UP " in f"{motion[-1]} "
