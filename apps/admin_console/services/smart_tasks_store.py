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

"""File-backed store for the console's recommended-task presets.

The shipped catalog lives in ``artemis/resources/config/smart_tasks.json`` and
is immutable. The first mutation copies the document into the writable config
directory (``config/smart_tasks.json`` in a source checkout), which keeps the
preset list editable both from the console and from an editor.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from artemis.config.paths import CONFIG_DIR, get_config_path
from artemis.resources import get_bundled_config_path

STORE_FILENAME = "smart_tasks.json"
STORE_VERSION = 1


class SmartTasksError(Exception):
    """Raised when the preset store cannot be read or a mutation is rejected."""


class SmartTasksNotFoundError(SmartTasksError):
    """Raised when a preset id does not exist in the catalog."""


class SmartTasksStore:
    """Reads and writes the preset document, falling back to the bundled copy."""

    def read_path(self) -> Path:
        """Resolve the file to read: the writable copy wins, else the shipped default."""
        writable = self.write_path()
        if writable.is_file():
            return writable
        # Fallback to bundled default
        bundled = get_bundled_config_path(STORE_FILENAME)
        if bundled is not None and Path(bundled).is_file():
            return Path(bundled)
        raise SmartTasksError(f"未找到推荐任务配置 {STORE_FILENAME}")

    def write_path(self) -> Path:
        """Resolve the file to mutate; the packaged default is never written to."""
        bundled = get_bundled_config_path(STORE_FILENAME)
        # Always prefer config/ directory for user edits
        user_path = CONFIG_DIR / STORE_FILENAME
        # If config/ file exists, use it
        if user_path.is_file():
            return user_path
        # Otherwise, check if get_config_path resolves to bundled
        try:
            resolved = get_config_path(STORE_FILENAME)
        except FileNotFoundError:
            return user_path
        if bundled is not None and resolved == Path(bundled):
            return user_path
        return resolved

    def read_raw(self) -> dict[str, Any]:
        """Return the whole document, raising a readable error on malformed JSON."""
        path = self.read_path()
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise SmartTasksError(f"读取 {path} 失败：{exc}") from exc
        if not isinstance(data, dict):
            raise SmartTasksError(f"{path} 的顶层结构必须是 JSON 对象")
        return data

    def list_tasks(self) -> list[Any]:
        """Return the raw preset entries in stored order."""
        tasks = self.read_raw().get("tasks")
        if not isinstance(tasks, list):
            raise SmartTasksError(f"{STORE_FILENAME} 缺少 tasks 数组")
        return tasks

    def list_apps(self) -> dict[str, Any]:
        """Return the app registry used for badges and package hints."""
        apps = self.read_raw().get("apps")
        return apps if isinstance(apps, dict) else {}

    def write_tasks(self, tasks: list[dict[str, Any]]) -> None:
        """Persist the preset list, keeping the app registry untouched."""
        path = self.write_path()
        payload = {
            "version": STORE_VERSION,
            "apps": self.list_apps(),
            "tasks": tasks,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except OSError as exc:
            raise SmartTasksError(f"写入 {path} 失败：{exc}") from exc

    def restore_defaults(self) -> None:
        """Drop the user copy so the bundled catalog is served again."""
        path = self.write_path()
        bundled = get_bundled_config_path(STORE_FILENAME)
        if bundled is not None and path == Path(bundled):
            raise SmartTasksError("当前已经是内置默认配置")
        if path.is_file():
            path.unlink()


smart_tasks_store = SmartTasksStore()
