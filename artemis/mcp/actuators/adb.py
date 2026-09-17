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

"""ADB-backed actuator: the reference implementation of the actuator contract.

Outcome messages describe the dispatched command, such as
"Tapped at [x, y] (normalized)." The agent verifies the effect from subsequent
observations.

Error convention: device-level refusals the controller *reports* (an ``error`` field,
a falsy success flag) come back as ``ActionResult(ok=False)`` with the historical
message. Unexpected exceptions are allowed to propagate -- the caller (executor or MCP
server layer) owns the ``"Error during X: {e}"`` wrapping, exactly as the original
executor's ``try/except`` did.
"""

import asyncio
import base64
import inspect
import io
import re
import time
from typing import Any

from PIL import Image

from artemis.context import ArtemisContext
from artemis.controllers.platform_specific_commands_controller import get_adb_device
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.mcp.action_manifest import DEVICE_ACTIONS, ExtensionTool
from artemis.mcp.action_types import ActionCode, ActionResult
from artemis.mcp.handwriting import (
    DEFAULT_STROKE_GAP_MS,
    HandwritingError,
    build_motion_scripts,
    render_text,
)
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["AdbActuator", "find_element_at_coords", "ensure_focus_at_coords"]


def find_element_at_coords(elements: list[dict], x: int, y: int) -> dict | None:
    """Finds the smallest (leaf-most) focusable element containing pixel [x, y].

    Moved from ``artemis.mcp.adb_server`` so both the stdio server and in-process
    actuator share one implementation.
    """
    matching_element = None
    min_area = float("inf")

    for elem in elements:
        is_focusable = (
            elem.get("focusable") == "true"
            or elem.get("clickable") == "true"
            or "EditText" in str(elem.get("class", ""))
        )
        if not is_focusable:
            continue

        bounds_str = elem.get("bounds")
        if bounds_str and isinstance(bounds_str, str):
            match = re.match(r"\[(\-?\d+),(\-?\d+)\]\[(\-?\d+),(\-?\d+)\]", bounds_str)
            if match:
                x1, y1, x2, y2 = map(int, match.groups())
                if x1 <= x <= x2 and y1 <= y <= y2:
                    area = (x2 - x1) * (y2 - y1)
                    if area < min_area:
                        min_area = area
                        matching_element = elem

    return matching_element


async def ensure_focus_at_coords(controller, x: int, y: int) -> str | None:
    """Ensures the element at pixel [x, y] is focused, tapping only if needed.

    Returns an error string on failure, ``None`` on success. Moved from
    ``artemis.mcp.adb_server``.
    """
    try:
        elements = await controller.get_ui_elements()
        elem = find_element_at_coords(elements, x, y)
        if elem and elem.get("focused") == "true":
            logger.info(f"Element under [{x}, {y}] is already focused. Skipping tap.")
            return None
    except Exception as e:
        logger.warning(f"Failed to check focus status: {e}. Falling back to unconditional tap.")

    result = await controller.tap_at(x=x, y=y)
    if hasattr(result, "error") and result.error:
        return result.error

    # Wait for the UI to settle and keyboard to pop up after tapping
    await asyncio.sleep(1.0)
    return None


class AdbActuator:
    """Drives an Android device through ``UnifiedMobileController`` over ADB."""

    def __init__(
        self,
        ctx: ArtemisContext,
        controller: UnifiedMobileController | None = None,
    ):
        self.ctx = ctx
        self.controller = controller or UnifiedMobileController(ctx)

    # --- Contract --------------------------------------------------------------------

    def capabilities(self) -> frozenset[str]:
        return DEVICE_ACTIONS

    def extensions(self) -> list[ExtensionTool]:
        return []

    # --- Coordinate helpers ----------------------------------------------------------

    def _dims(self) -> tuple[int, int]:
        width = getattr(self.ctx.device, "device_width", 1080) if self.ctx.device else 1080
        height = getattr(self.ctx.device, "device_height", 2400) if self.ctx.device else 2400
        return width or 1080, height or 2400

    def _to_px(self, nx: int, ny: int) -> tuple[int, int]:
        width, height = self._dims()
        x = int(max(0, min(width - 1, int(nx) * width / 1000)))
        y = int(max(0, min(height - 1, int(ny) * height / 1000)))
        return x, y

    # --- Required action -------------------------------------------------------------

    async def click_sequence(
        self, points: list[tuple[int, int]], delay_ms: int = 50
    ) -> ActionResult:
        outcomes = []
        for i, (nx, ny) in enumerate(points):
            x, y = self._to_px(nx, ny)
            result = await self.controller.tap_at(x, y)
            err = getattr(result, "error", None) if result else None
            if err:
                return ActionResult.failure(
                    "click_sequence",
                    f"Error executing click at step {i + 1}: {err}",
                    detail=str(err),
                )
            outcomes.append(f"[{nx}, {ny}]")
            if i < len(points) - 1:
                await asyncio.sleep(max(0, delay_ms) / 1000.0)

        return ActionResult.success(
            "click_sequence",
            f"Tapped in sequence at {'; '.join(outcomes)} (normalized).",
        )

    # --- Optional actions ------------------------------------------------------------

    async def click(self, nx: int, ny: int, times: int = 1, delay_ms: int = 100) -> ActionResult:
        x, y = self._to_px(nx, ny)
        result = await self.controller.tap_at(x, y, times=times, delay_ms=delay_ms)
        err = getattr(result, "error", None) if result else None
        if err is not None:
            return ActionResult.failure("click", f"Error executing click: {err}", detail=str(err))
        return ActionResult.success(
            "click",
            f"Tapped at [{nx}, {ny}] (normalized).",
            normalized_coordinates=[int(nx), int(ny)],
        )

    async def long_press(self, nx: int, ny: int, duration_ms: int = 1000) -> ActionResult:
        x, y = self._to_px(nx, ny)
        result = await self.controller.tap_at(
            x, y, long_press=True, long_press_duration=duration_ms
        )
        err = getattr(result, "error", None) if result else None
        if err is not None:
            return ActionResult.failure(
                "long_press", f"Error executing long press: {err}", detail=str(err)
            )
        return ActionResult.success(
            "long_press",
            f"Long-pressed at [{nx}, {ny}] (normalized) for {duration_ms}ms.",
            normalized_coordinates=[int(nx), int(ny)],
            duration_ms=duration_ms,
        )

    async def input_text(
        self,
        text: str,
        target: tuple[int, int] | None = None,
        clear_exist: bool = True,
    ) -> ActionResult:
        coords = None
        if target:
            nx, ny = target
            x, y = self._to_px(nx, ny)
            coords = [int(nx), int(ny)]
            err = await ensure_focus_at_coords(self.controller, x, y)
            if err:
                return ActionResult.failure(
                    "input_text",
                    f"Error focusing element: {err}",
                    detail=str(err),
                    normalized_coordinates=coords,
                )
        if clear_exist:
            if hasattr(self.controller, "erase_text") and not await self.controller.erase_text():
                return ActionResult.failure(
                    "input_text",
                    "Failed to clear existing text",
                    normalized_coordinates=coords,
                )
        elif hasattr(self.controller, "press_key"):
            # KEYCODE_MOVE_END: move cursor to the end for reliable append.
            await self.controller.press_key("123")
        if hasattr(self.controller, "type_text") and not await self.controller.type_text(
            text, clear_existing=False
        ):
            return ActionResult.failure(
                "input_text",
                f"Failed typing '{text}'.",
                normalized_coordinates=coords,
            )

        return ActionResult.success(
            "input_text",
            f"Typed '{text}'"
            + (f" at [{coords[0]}, {coords[1]}] (normalized)." if coords else "."),
            normalized_coordinates=coords,
        )

    async def swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int = 800,
    ) -> ActionResult:
        nx1, ny1 = start
        nx2, ny2 = end
        px1, py1 = self._to_px(nx1, ny1)
        px2, py2 = self._to_px(nx2, ny2)
        err = await self.controller.swipe_coords(px1, py1, px2, py2, duration_ms)
        if err:
            return ActionResult.failure("swipe", f"Error dragging: {err}", detail=str(err))
        return ActionResult.success(
            "swipe",
            f"Swiped from [{nx1}, {ny1}] to [{nx2}, {ny2}] (normalized).",
            normalized_coordinates=[int(nx1), int(ny1), int(nx2), int(ny2)],
            duration_ms=duration_ms,
        )

    #: Side of the default canvas box, as a fraction of the smaller screen edge.
    _DEFAULT_CANVAS_FRACTION = 0.32

    def _canvas_box(
        self, bounds: tuple[int, int, int, int] | None
    ) -> tuple[tuple[int, int, int, int] | None, str | None]:
        """Resolve normalized canvas bounds to pixels, or the default box.

        Returns ``(box, error)``: ``error`` is set when the caller named a box
        that cannot hold a glyph, so a bad target is reported instead of being
        silently replaced by the default region.
        """
        if bounds:
            if len(bounds) != 4:
                return None, f"Canvas bounds must be [left, top, right, bottom], got {bounds}."
            nx1, ny1 = self._to_px(int(bounds[0]), int(bounds[1]))
            nx2, ny2 = self._to_px(int(bounds[2]), int(bounds[3]))
            left, right = sorted((nx1, nx2))
            top, bottom = sorted((ny1, ny2))
            if right - left < 24 or bottom - top < 24:
                return None, (
                    f"Canvas bounds {list(bounds)} (normalized) resolve to a"
                    f" {right - left}x{bottom - top}px box, too small to write in."
                )
            return (left, top, right, bottom), None

        width, height = self._dims()
        side = max(64, int(min(width, height) * self._DEFAULT_CANVAS_FRACTION))
        cx, cy = width // 2, height // 2
        return (cx - side // 2, cy - side // 2, cx + side // 2, cy + side // 2), None

    #: Fraction of dark pixels that counts as "ink appeared" in the canvas region.
    _INK_EPSILON = 0.002

    async def _fresh_screenshot(self) -> Any:
        """A current frame, or ``None`` when it cannot be read.

        The accessibility helper serves a *cached* frame: it reported an unchanged
        canvas for strokes that had in fact landed, so the blank-canvas check reads
        the frame straight from adb and only falls back to the controller.
        """
        try:
            device = get_adb_device(self.ctx)
            if device is not None:
                shot = await asyncio.to_thread(device.screenshot)
                if hasattr(shot, "crop"):
                    return shot
            raw = await self.controller.take_screenshot()
            return Image.open(io.BytesIO(base64.b64decode(raw)))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            logger.debug(f"Canvas ink check skipped: {exc}")
            return None

    async def _canvas_ink(self, box: tuple[int, int, int, int]) -> float | None:
        """Fraction of dark pixels in the canvas region, or ``None`` when unreadable.

        A handwriting canvas is near-white before the first stroke, so the ink
        fraction is the honest signal for "did anything get drawn" -- a byte
        comparison also reacts to carets and animations, and cannot tell a fresh
        frame from a stale one.
        """
        image = await self._fresh_screenshot()
        if image is None:
            return None
        crop = image.crop(box).convert("L")
        pixels = crop.tobytes()
        dark = sum(1 for value in pixels if value < 128)
        return dark / max(1, len(pixels))

    async def _draw_strokes(self, scripts: list[str], total_points: int) -> tuple[bool, str | None]:
        """Run the motion scripts on the device. Returns ``(ok, error)``.

        Device-level exceptions propagate: this module's convention is that the
        caller owns the ``Error during handwrite: {e}`` wrapping.
        """
        device = get_adb_device(self.ctx)
        if device is None:
            return False, "No ADB device connection is available for handwriting."

        loop = asyncio.get_running_loop()
        # Measured on a Pixel-class device: ~50ms per injected point, because every
        # event is one `input` process on the device. Budget 2.4x that plus overhead.
        deadline = loop.time() + 20.0 + 0.12 * total_points
        for index, script in enumerate(scripts, start=1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, f"Handwriting ran out of time before script {index}/{len(scripts)}."
            try:
                await asyncio.wait_for(asyncio.to_thread(device.shell, script), timeout=remaining)
            except TimeoutError:
                return False, f"Handwriting timed out on script {index}/{len(scripts)}."
            except OSError as exc:
                return False, f"Handwriting failed on script {index}/{len(scripts)}: {exc}"
        return True, None

    async def handwrite(
        self,
        text: str,
        bounds: tuple[int, int, int, int] | None = None,
        source: str = "auto",
        point_interval_ms: int = 0,
        stroke_gap_ms: int = DEFAULT_STROKE_GAP_MS,
    ) -> ActionResult:
        """Write ``text`` onto a canvas as real pen strokes.

        A handwriting canvas ignores typed text, and a pen-only canvas ignores
        finger events: ``source="auto"`` injects pen (stylus) events and retries
        with finger events when the canvas stays blank, so the caller does not
        have to know which kind of canvas it is facing.
        """
        box, box_error = self._canvas_box(bounds)
        if box is None:
            return ActionResult.failure("handwrite", box_error or "", code=ActionCode.INVALID_ARGS)

        try:
            strokes = render_text(text, box)
            scripts = build_motion_scripts(
                strokes,
                source=source,
                point_interval_ms=point_interval_ms,
                stroke_gap_ms=stroke_gap_ms,
            )
        except HandwritingError as exc:
            return ActionResult.failure("handwrite", str(exc), code=ActionCode.INVALID_ARGS)

        total_points = sum(len(stroke) for stroke in strokes)
        if not scripts:
            return ActionResult.failure(
                "handwrite", f"Nothing to write for '{text}'.", code=ActionCode.INVALID_ARGS
            )

        verify = source == "auto"
        before = await self._canvas_ink(box) if verify else None
        attempt = "stylus" if source == "auto" else source

        ok, error = await self._draw_strokes(scripts, total_points)
        if not ok:
            return ActionResult.failure("handwrite", error or "Handwriting failed.")

        if verify and before is not None:
            after = await self._canvas_ink(box)
            if after is not None and after <= before + self._INK_EPSILON:
                # The canvas ignored pen events: some canvases are finger-only.
                logger.info("Handwriting left the canvas unchanged; retrying with finger events.")
                finger_scripts = build_motion_scripts(
                    strokes,
                    source="touchscreen",
                    point_interval_ms=point_interval_ms,
                    stroke_gap_ms=stroke_gap_ms,
                )
                ok, error = await self._draw_strokes(finger_scripts, total_points)
                if not ok:
                    return ActionResult.failure("handwrite", error or "Handwriting failed.")
                attempt = "touchscreen"
                after = await self._canvas_ink(box)
                if after is not None and after <= before + self._INK_EPSILON:
                    return ActionResult.failure(
                        "handwrite",
                        f"Wrote '{text}' but the canvas did not change with pen or finger"
                        " events. Check that the target box is the writing area (and that"
                        " the canvas is ready for input), then retry.",
                    )

        return ActionResult.success(
            "handwrite",
            f"Hand-wrote '{text}' as {len(strokes)} stroke(s) / {total_points} pen"
            f" points into {list(box)}px using {attempt} events.",
            duration_ms=int(0.05 * total_points),
        )

    async def press_key(self, key: str) -> ActionResult:
        key_str = str(key).lower()
        if not key_str:
            return ActionResult.failure(
                "press_key",
                f"Error executing key press '{key}'.",
                code=ActionCode.INVALID_ARGS,
            )

        if key_str == "back" and hasattr(self.controller, "go_back"):
            await self.controller.go_back()
        elif key_str == "home" and hasattr(self.controller, "go_home"):
            await self.controller.go_home()
        elif key_str == "enter" and hasattr(self.controller, "press_enter"):
            await self.controller.press_enter()
        elif hasattr(self.controller, "press_key"):
            # Pass unrecognized keycodes through verbatim (KEYCODE_* names, numeric
            # codes): the driver resolves or forwards them, matching the historical
            # adb_server behavior of accepting any Android key event.
            res = await self.controller.press_key(key)
            err = getattr(res, "error", None) if res else None
            if err:
                return ActionResult.failure(
                    "press_key",
                    f"Error executing key press '{key}': {err}",
                    detail=str(err),
                )
            if not res:
                return ActionResult.failure("press_key", f"Error executing key press '{key}'.")

        return ActionResult.success("press_key", f"Pressed key '{key}'.")

    async def manage_app(self, action: str, app_name: str) -> ActionResult:
        # Imported lazily: launch_app pulls in tool wrappers that are costly at import.
        from artemis.tools.mobile.launch_app import find_package
        from artemis.utils.app_launch_utils import launch_app_with_retries

        res = find_package(self.ctx, app_name, use_fallback=False)
        pkg = await res if inspect.iscoroutine(res) else res
        if not pkg:
            return ActionResult.failure(
                "manage_app",
                f"Error finding package for app: {app_name}",
                code=ActionCode.PACKAGE_NOT_FOUND,
            )
        target_pkg = pkg

        if action.lower() == "launch":
            res_launch = launch_app_with_retries(self.ctx, target_pkg)
            if inspect.iscoroutine(res_launch):
                res_launch = await res_launch
            if isinstance(res_launch, tuple):
                success, error_msg = res_launch
            else:
                success, error_msg = bool(res_launch), ""

            if success:
                return ActionResult.success(
                    "manage_app",
                    f"Launched app '{app_name}' ({target_pkg}); foreground confirmed.",
                )
            return ActionResult.failure(
                "manage_app",
                f"Failed to launch app '{app_name}': {error_msg}",
                detail=error_msg or None,
            )
        elif action.lower() == "stop":
            if hasattr(self.controller, "terminate_app"):
                res_term = self.controller.terminate_app(target_pkg)
                if inspect.iscoroutine(res_term):
                    await res_term
            return ActionResult.success(
                "manage_app", f"Force-stopped app '{app_name}' ({target_pkg})."
            )
        return ActionResult.failure(
            "manage_app",
            f"Invalid manage_app action: {action}",
            code=ActionCode.INVALID_ARGS,
        )

    async def wait_for_delay(self, time_in_ms: int) -> ActionResult:
        delay_s = max(0.0, float(time_in_ms) / 1000.0)
        await asyncio.sleep(delay_s)
        return ActionResult.success(
            "wait_for_delay",
            f"Waited {time_in_ms}ms.",
            duration_ms=int(time_in_ms),
        )

    async def wait_for_text(
        self, text: str, wait_state: str | None = None, timeout_ms: int | None = None
    ) -> ActionResult:
        timeout_s = (timeout_ms or 5000) / 1000.0
        start = time.time()
        found = False
        target_state = (wait_state or "appear").lower()

        while time.time() - start < timeout_s:
            tree = await self.controller.get_ui_elements()
            text_present = text.lower() in str(tree or "").lower()
            if target_state == "appear" and text_present:
                found = True
                break
            elif target_state == "disappear" and not text_present:
                found = True
                break
            await asyncio.sleep(0.5)

        if found:
            return ActionResult.success(
                "wait_for_text",
                f"Text '{text}' {'appeared' if target_state == 'appear' else 'disappeared'}"
                f" in the UI tree after {int((time.time() - start) * 1000)}ms.",
            )
        return ActionResult.failure(
            "wait_for_text",
            f"Timed out waiting for text '{text}' to {target_state}.",
            code=ActionCode.TIMEOUT,
        )

    async def open_link(self, url: str) -> ActionResult:
        success = await self.controller.open_url(url)
        if success:
            return ActionResult.success("open_link", f"Opened link '{url}'.")
        return ActionResult.failure("open_link", f"Failed to open link '{url}'.")

    async def erase_one_char(self) -> ActionResult:
        success = await self.controller.erase_text(nb_chars=1)
        if success:
            return ActionResult.success("erase_one_char", "Erased one character.")
        return ActionResult.failure("erase_one_char", "Failed to erase one character.")

    async def focus_and_clear_text(self, nx: int, ny: int) -> ActionResult:
        x, y = self._to_px(nx, ny)
        err = await ensure_focus_at_coords(self.controller, x, y)
        if err:
            return ActionResult.failure(
                "focus_and_clear_text",
                f"Error focusing element: {err}",
                detail=str(err),
                normalized_coordinates=[int(nx), int(ny)],
            )
        success = await self.controller.erase_text()
        if success:
            return ActionResult.success(
                "focus_and_clear_text",
                f"Cleared text at [{nx}, {ny}] (normalized).",
                normalized_coordinates=[int(nx), int(ny)],
            )
        return ActionResult.failure("focus_and_clear_text", "Failed to erase text.")

    # --- Internal observation primitives ---------------------------------------------

    async def take_screenshot(self) -> str:
        return await self.controller.take_screenshot()

    async def get_ui_elements(self) -> Any:
        return await self.controller.get_ui_elements()

    async def get_screen_data(self) -> Any:
        return await self.controller.get_screen_data()
