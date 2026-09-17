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

"""The actuator's handwrite action: canvas resolution and the input-source retry.

A handwriting canvas may be pen-only (pen events required) or finger-only, and
the caller should not have to know which. ``source="auto"`` injects pen events
and falls back to finger events when the canvas region stays byte-identical.
"""

import base64
import io
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
import pytest

from artemis.mcp import handwriting
from artemis.mcp.actuators.adb import AdbActuator
from artemis.mcp.handwriting import stroke_db_path

requires_db = pytest.mark.skipif(stroke_db_path() is None, reason="strokes.db is not installed")

SCREEN = (1000, 2000)


def _png(color: int) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (SCREEN[0], SCREEN[1]), (color, color, color)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _actuator(shots=None):
    ctx = Mock()
    ctx.device.device_width = SCREEN[0]
    ctx.device.device_height = SCREEN[1]
    controller = Mock()
    shots = list(shots or [_png(255), _png(255)])
    controller.take_screenshot = AsyncMock(side_effect=lambda: shots.pop(0) if shots else _png(255))
    return AdbActuator(ctx, controller=controller), controller


def _fake_device():
    device = Mock()
    device.shell = Mock(return_value="")
    return device


@pytest.mark.asyncio
async def test_default_canvas_is_a_centered_square_of_the_smaller_edge():
    actuator, _ = _actuator()
    with patch("artemis.mcp.actuators.adb.get_adb_device", return_value=_fake_device()):
        result = await actuator.handwrite("毯", source="stylus")

    assert result.ok, result.message
    # 32% of min(1000, 2000) = 320px, centered on a 1000x2000 screen.
    assert "[340, 840, 660, 1160]px" in result.message


@pytest.mark.asyncio
async def test_explicit_bounds_are_mapped_to_pixels():
    actuator, _ = _actuator()
    device = _fake_device()
    with patch("artemis.mcp.actuators.adb.get_adb_device", return_value=device):
        result = await actuator.handwrite("毯", bounds=(100, 100, 500, 500), source="stylus")

    assert result.ok, result.message
    assert "[100, 200, 500, 1000]px" in result.message
    # Every stroke is one gesture: the injected script must open and close them.
    script = device.shell.call_args.args[0]
    assert " DOWN " in script and " UP " in script


@pytest.mark.asyncio
async def test_a_box_too_small_to_write_in_is_rejected():
    actuator, _ = _actuator()
    with patch("artemis.mcp.actuators.adb.get_adb_device", return_value=_fake_device()):
        result = await actuator.handwrite("毯", bounds=(500, 500, 505, 505))

    assert not result.ok
    assert "too small" in result.message


@pytest.mark.asyncio
async def test_no_device_connection_is_reported():
    actuator, _ = _actuator()
    with patch("artemis.mcp.actuators.adb.get_adb_device", return_value=None):
        result = await actuator.handwrite("毯", source="stylus")

    assert not result.ok
    assert "No ADB device connection" in result.message


@pytest.mark.asyncio
async def test_auto_keeps_pen_events_when_the_canvas_changes():
    # before/after screenshots differ: the pen worked, no fallback.
    actuator, _ = _actuator(shots=[_png(255), _png(0)])
    device = _fake_device()
    with patch("artemis.mcp.actuators.adb.get_adb_device", return_value=device):
        result = await actuator.handwrite("毯", bounds=(100, 100, 500, 500))

    assert result.ok, result.message
    assert "using stylus events" in result.message
    # No fallback happened: every script that reached the device used pen events.
    assert device.shell.call_count >= 1
    assert all("input stylus motionevent" in c.args[0] for c in device.shell.call_args_list)


@pytest.mark.asyncio
async def test_auto_retries_with_finger_events_when_the_canvas_stays_blank():
    # before/after identical for the pen attempt, then different for the finger one.
    actuator, _ = _actuator(shots=[_png(255), _png(255), _png(0)])
    device = _fake_device()
    with patch("artemis.mcp.actuators.adb.get_adb_device", return_value=device):
        result = await actuator.handwrite("毯", bounds=(100, 100, 500, 500))

    assert result.ok, result.message
    assert "using touchscreen events" in result.message
    scripts = [call.args[0] for call in device.shell.call_args_list]
    assert any("input stylus motionevent" in script for script in scripts)
    finger = [s for s in scripts if "input motionevent" in s and "stylus" not in s]
    assert finger, "expected a finger-event retry after the blank pen attempt"


@pytest.mark.asyncio
async def test_auto_reports_when_neither_source_leaves_ink():
    actuator, _ = _actuator(shots=[_png(255)] * 6)
    with patch("artemis.mcp.actuators.adb.get_adb_device", return_value=_fake_device()):
        result = await actuator.handwrite("毯", bounds=(100, 100, 500, 500))

    assert not result.ok
    assert "did not change" in result.message


@requires_db
@pytest.mark.asyncio
async def test_an_unknown_character_is_rejected_before_touching_the_device():
    actuator, _ = _actuator()
    device = _fake_device()
    with patch("artemis.mcp.actuators.adb.get_adb_device", return_value=device):
        result = await actuator.handwrite("毯A", bounds=(100, 100, 500, 500))

    assert not result.ok
    assert "No stroke data" in result.message
    device.shell.assert_not_called()


@pytest.mark.asyncio
async def test_a_missing_database_is_reported(monkeypatch):
    monkeypatch.setattr(handwriting, "stroke_db_path", lambda: None)
    handwriting._medians_for.cache_clear()
    actuator, _ = _actuator()
    try:
        result = await actuator.handwrite("毯", bounds=(100, 100, 500, 500))
        assert not result.ok
        assert "strokes.db" in result.message
    finally:
        handwriting._medians_for.cache_clear()
