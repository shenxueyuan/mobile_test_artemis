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

"""Preset Task Catalog & Dynamic Recommendation Engine for Artemis.

The catalog is file-backed (see ``smart_tasks_store``) so the console can add,
edit and remove presets at runtime; ``artemis/resources/config/smart_tasks.json``
holds the shipped defaults and ``config/smart_tasks.json`` the user's edits.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

try:
    from admin_console.services.smart_tasks_store import (
        SmartTasksError,
        SmartTasksNotFoundError,
        smart_tasks_store,
    )
except ImportError:
    from apps.admin_console.services.smart_tasks_store import (
        SmartTasksError,
        SmartTasksNotFoundError,
        smart_tasks_store,
    )


class AppInfo(BaseModel):
    """Application metadata."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str
    icon: str = "apps"
    pkg: str | None = None
    category: str = "general"


class TaskPreset(BaseModel):
    """Smart suggestion task definition."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=300)
    goal: str = Field(min_length=1)
    profile: Literal["flash", "pro"] = "flash"
    category: Literal["flash", "pro", "cross_app", "monitor"] = "flash"
    tag: str = Field(default="", max_length=60)
    apps: list[AppInfo] = Field(default_factory=list)
    required_packages: list[str] = Field(default_factory=list, alias="requiredPackages")
    match_mode: Literal["any", "all"] = Field(default="any", alias="matchMode")
    priority: int = Field(default=50, ge=0, le=100)

    def wire(self) -> dict[str, Any]:
        """Return the camelCase payload the console consumes."""
        return self.model_dump(by_alias=True)


def _slugify(value: str) -> str:
    """Build an ASCII id from a title; empty when the title has no ASCII letters."""
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _load_presets() -> list[TaskPreset]:
    """Validate every stored entry, naming the offending row on failure."""
    presets: list[TaskPreset] = []
    for index, entry in enumerate(smart_tasks_store.list_tasks()):
        try:
            presets.append(TaskPreset.model_validate(entry))
        except ValidationError as exc:
            raise SmartTasksError(f"第 {index + 1} 个推荐任务不合法：{exc}") from exc
    return presets


def _persist(presets: list[TaskPreset]) -> None:
    """Write the given presets back to the user store."""
    smart_tasks_store.write_tasks([preset.wire() for preset in presets])


def build_preset(payload: dict[str, Any]) -> TaskPreset:
    """Validate a console payload, deriving a slug id when none is supplied."""
    data = dict(payload)
    if not str(data.get("id") or "").strip():
        slug = _slugify(str(data.get("title") or ""))
        data["id"] = slug or f"task_{uuid4().hex[:8]}"
    try:
        return TaskPreset.model_validate(data)
    except ValidationError as exc:
        raise SmartTasksError(f"推荐任务配置不合法：{exc}") from exc


def list_presets() -> list[TaskPreset]:
    """Return every preset in stored order."""
    return _load_presets()


def create_preset(payload: dict[str, Any]) -> TaskPreset:
    """Append a new preset, rejecting a duplicate id."""
    preset = build_preset(payload)
    presets = _load_presets()
    if any(existing.id == preset.id for existing in presets):
        raise SmartTasksError(f"任务 id 已存在：{preset.id}")
    presets.append(preset)
    _persist(presets)
    return preset


def update_preset(task_id: str, payload: dict[str, Any]) -> TaskPreset:
    """Replace an existing preset, keeping its position in the list."""
    presets = _load_presets()
    index = next((i for i, item in enumerate(presets) if item.id == task_id), None)
    if index is None:
        raise SmartTasksNotFoundError(f"未找到推荐任务：{task_id}")
    data = dict(payload)
    if not str(data.get("id") or "").strip():
        data["id"] = task_id
    preset = build_preset(data)
    if preset.id != task_id and any(item.id == preset.id for item in presets):
        raise SmartTasksError(f"任务 id 已存在：{preset.id}")
    presets[index] = preset
    _persist(presets)
    return preset


def delete_preset(task_id: str) -> None:
    """Remove a preset, rejecting an unknown id."""
    presets = _load_presets()
    remaining = [item for item in presets if item.id != task_id]
    if len(remaining) == len(presets):
        raise SmartTasksNotFoundError(f"未找到推荐任务：{task_id}")
    _persist(remaining)


def reset_presets() -> list[TaskPreset]:
    """Restore the shipped catalog by dropping the user copy."""
    smart_tasks_store.restore_defaults()
    return _load_presets()


class TaskRecommendationEngine:
    """Intelligent recommendation engine matching device capabilities."""

    def __init__(self, catalog: list[TaskPreset] | None = None):
        self._static_catalog = catalog

    @property
    def catalog(self) -> list[TaskPreset]:
        """Presets to recommend; re-read on access so console edits take effect."""
        return self._static_catalog if self._static_catalog is not None else _load_presets()

    def get_all_tasks(self) -> list[dict[str, Any]]:
        """Return every preset as a wire-ready dict."""
        return [task.wire() for task in self.catalog]

    def get_app_registry(self) -> dict[str, Any]:
        """Return the app registry shipped alongside the presets."""
        return smart_tasks_store.list_apps()

    def recommend_tasks(
        self, installed_packages: list[str] | set[str], category: str = "all", limit: int = 12
    ) -> list[dict[str, Any]]:
        pkgs_set = (
            set(installed_packages) if isinstance(installed_packages, list) else installed_packages
        )

        scored_tasks: list[tuple[int, TaskPreset, bool]] = []

        for task in self.catalog:
            # Check package presence
            is_matched = False
            if pkgs_set:
                if not task.required_packages:
                    is_matched = True
                elif task.match_mode == "all":
                    is_matched = all(pkg in pkgs_set for pkg in task.required_packages)
                else:
                    is_matched = any(pkg in pkgs_set for pkg in task.required_packages)
            else:
                is_matched = False

            # Calculate score
            score = task.priority
            if pkgs_set:
                if is_matched:
                    score += 100
                    if len(task.required_packages) > 1 and task.match_mode == "all":
                        score += 30
                else:
                    score -= 40

            # Filter by category
            if category == "flash" and task.profile != "flash":
                continue
            elif category == "pro" and task.profile != "pro":
                continue
            elif category == "cross_app" and task.category != "cross_app":
                continue
            elif category == "monitor" and task.category != "monitor":
                continue

            scored_tasks.append((score, task, is_matched))

        # Sort descending by score
        scored_tasks.sort(key=lambda item: item[0], reverse=True)

        results: list[dict[str, Any]] = []
        for _, task, matched in scored_tasks[:limit]:
            payload = task.wire()
            payload["is_device_matched"] = matched
            results.append(payload)

        return results


task_recommendation_engine = TaskRecommendationEngine()
