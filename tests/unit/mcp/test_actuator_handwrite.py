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
and falls back to finger events when the canvas shows no new ink.
"""

from unittest.mock import Mock, patch

from PIL import Image
import pytest

from artemis.mcp import handwriting
from artemis.mcp.actuators.adb import AdbActuator
from artemis.mcp.handwriting import stroke_db_path

requires_db = pytest.mark.skipif(stroke_db_path() is None, reason="strokes.db is not installed")

SCREEN = (1000, 2000)
#: A box the tests write into, in normalized 0-1000 units.
BOUNDS = (100, 100, 500, 500)


def _blank() -> Image.Image:
    """An empty (white) canvas screen."""
    return Image.new("RGB", SCREEN, (255, 255, 255))


def _inked() -> Image.Image:
    """The same screen with a dark mark inside the writing box."""
    image = _blank()
    left, top = 100, 200
    for x in range(left, left + 80):
        for y in range(top, top + 80):
            image.putpixel((x, y), (0, 0, 0))
    return image


def _actuator(images=None):
    ctx = Mock()
    ctx.device.device_width = SCREEN[0]
    ctx.device.device_height = SCREEN[1]
    controller = Mock()
    controller.take_screenshot = Mock(side_effect=AssertionError("stale frame path used"))
    return AdbActuator(ctx, controller=controller), controller


def _fake_device(images=None):
    """An adb device whose screenshots walk through ``images``."""
    frames = list(images or [])
    device = Mock()
    device.shell = Mock(return_value="")
    device.screenshot = Mock(side_effect=lambda: frames.pop(0) if frames else _blank())
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
        result = await actuator.handwrite("毯", bounds=BOUNDS, source="stylus")

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
async def test_auto_keeps_pen_events_when_ink_appears():
    # Blank before, marked after: the pen worked, so no fallback.
    actuator, _ = _actuator()
    device = _fake_device(images=[_blank(), _inked()])
    with patch("artemis.mcp.actuators.adb.get_adb_device", return_value=device):
        result = await actuator.handwrite("毯", bounds=BOUNDS)

    assert result.ok, result.message
    assert "using stylus events" in result.message
    # No fallback happened: every script that reached the device used pen events.
    assert device.shell.call_count >= 1
    assert all("input stylus motionevent" in c.args[0] for c in device.shell.call_args_list)


@pytest.mark.asyncio
async def test_auto_retries_with_finger_events_when_no_ink_appears():
    # Blank for the pen attempt, marked once the finger attempt lands.
    actuator, _ = _actuator()
    device = _fake_device(images=[_blank(), _blank(), _inked()])
    with patch("artemis.mcp.actuators.adb.get_adb_device", return_value=device):
        result = await actuator.handwrite("毯", bounds=BOUNDS)

    assert result.ok, result.message
    assert "using touchscreen events" in result.message
    scripts = [call.args[0] for call in device.shell.call_args_list]
    assert any("input stylus motionevent" in script for script in scripts)
    finger = [s for s in scripts if "input motionevent" in s and "stylus" not in s]
    assert finger, "expected a finger-event retry after the blank pen attempt"


@pytest.mark.asyncio
async def test_auto_reports_when_neither_source_leaves_ink():
    actuator, _ = _actuator()
    device = _fake_device(images=[_blank()] * 8)
    with patch("artemis.mcp.actuators.adb.get_adb_device", return_value=device):
        result = await actuator.handwrite("毯", bounds=BOUNDS)

    assert not result.ok
    assert "did not change" in result.message


@pytest.mark.asyncio
async def test_an_unreadable_frame_skips_the_check_instead_of_failing():
    """Verification must never fail an action whose strokes were already drawn."""
    actuator, _ = _actuator()
    device = _fake_device()
    device.screenshot = Mock(side_effect=OSError("device offline"))
    with patch("artemis.mcp.actuators.adb.get_adb_device", return_value=device):
        result = await actuator.handwrite("毯", bounds=BOUNDS)

    assert result.ok, result.message
    assert "using stylus events" in result.message


@requires_db
@pytest.mark.asyncio
async def test_an_unknown_character_is_rejected_before_touching_the_device():
    actuator, _ = _actuator()
    device = _fake_device()
    with patch("artemis.mcp.actuators.adb.get_adb_device", return_value=device):
        result = await actuator.handwrite("毯A", bounds=BOUNDS)

    assert not result.ok
    assert "No stroke data" in result.message
    device.shell.assert_not_called()


@pytest.mark.asyncio
async def test_a_missing_database_is_reported(monkeypatch):
    monkeypatch.setattr(handwriting, "stroke_db_path", lambda: None)
    handwriting._medians_for.cache_clear()
    actuator, _ = _actuator()
    try:
        result = await actuator.handwrite("毯", bounds=BOUNDS)
        assert not result.ok
        assert "strokes.db" in result.message
    finally:
        handwriting._medians_for.cache_clear()
