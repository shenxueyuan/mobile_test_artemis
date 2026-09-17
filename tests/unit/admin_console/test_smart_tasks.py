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

"""Tests for the file-backed recommended-task preset store and its CRUD API."""

import json

from fastapi import HTTPException
import pytest

import apps.admin_console.services.smart_tasks_store as store_module
from apps.admin_console.routers import tasks as tasks_router
from apps.admin_console.services.smart_tasks_store import (
    SmartTasksError,
    SmartTasksNotFoundError,
    SmartTasksStore,
)
from apps.admin_console.services.task_preset_catalog import (
    create_preset,
    delete_preset,
    list_presets,
    reset_presets,
    update_preset,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolate mutations in a temporary config directory."""
    monkeypatch.setattr(store_module, "CONFIG_DIR", tmp_path)
    return SmartTasksStore()


def _new_task(**overrides):
    payload = {
        "title": "打开蓝牙并连接耳机",
        "description": "在设置中开启蓝牙",
        "goal": "打开设置，开启蓝牙，并连接已配对的耳机。",
        "profile": "flash",
        "category": "flash",
        "tag": "设置",
        "requiredPackages": ["com.android.settings"],
        "priority": 70,
    }
    payload.update(overrides)
    return payload


def test_reads_bundled_defaults_when_no_user_copy_exists(store):
    presets = list_presets()

    assert len(presets) >= 10
    assert any(preset.id == "maps_coffee" for preset in presets)
    # The console consumes camelCase keys.
    assert "requiredPackages" in presets[0].wire()


def test_create_derives_an_id_from_an_ascii_title(store):
    created = create_preset(_new_task(title="Toggle Bluetooth"))

    assert created.id == "toggle_bluetooth"
    assert store.write_path().is_file()
    assert any(preset.id == "toggle_bluetooth" for preset in list_presets())


def test_create_derives_a_generated_id_for_a_non_ascii_title(store):
    created = create_preset(_new_task())

    assert created.id.startswith("task_")
    assert len(created.id) == len("task_") + 8


def test_create_rejects_a_duplicate_id(store):
    create_preset(_new_task(id="dup_task"))

    with pytest.raises(SmartTasksError, match="已存在"):
        create_preset(_new_task(id="dup_task"))


def test_create_rejects_a_payload_without_a_goal(store):
    with pytest.raises(SmartTasksError, match="不合法"):
        create_preset(_new_task(goal=""))


def test_update_replaces_the_preset_in_place(store):
    create_preset(_new_task(id="keep_order", priority=60))
    before = [preset.id for preset in list_presets()]

    updated = update_preset("keep_order", _new_task(id="keep_order", title="改过的标题"))

    assert updated.title == "改过的标题"
    assert [preset.id for preset in list_presets()] == before


def test_update_keeps_the_original_id_when_the_payload_omits_it(store):
    create_preset(_new_task(id="stable_id"))

    updated = update_preset("stable_id", _new_task(title="换个标题"))

    assert updated.id == "stable_id"


def test_update_rejects_an_unknown_id(store):
    with pytest.raises(SmartTasksNotFoundError):
        update_preset("missing_id", _new_task())


def test_delete_removes_only_the_requested_preset(store):
    create_preset(_new_task(id="to_remove"))
    create_preset(_new_task(id="to_keep"))

    delete_preset("to_remove")

    remaining = [preset.id for preset in list_presets()]
    assert "to_keep" in remaining
    assert "to_remove" not in remaining


def test_delete_rejects_an_unknown_id(store):
    with pytest.raises(SmartTasksNotFoundError):
        delete_preset("missing_id")


def test_reset_restores_the_bundled_catalog(store):
    create_preset(_new_task(id="temporary"))
    assert store.write_path().is_file()

    restored = reset_presets()

    assert not store.write_path().exists()
    assert not any(preset.id == "temporary" for preset in restored)
    assert any(preset.id == "maps_coffee" for preset in restored)


def test_mutations_never_touch_the_packaged_defaults(store):
    bundled = store_module.get_bundled_config_path(store_module.STORE_FILENAME)
    assert bundled is not None
    before = json.dumps(json.loads(bundled.read_text(encoding="utf-8")), sort_keys=True)

    create_preset(_new_task(id="isolated"))
    delete_preset("isolated")

    after = json.dumps(json.loads(bundled.read_text(encoding="utf-8")), sort_keys=True)
    assert before == after


@pytest.mark.asyncio
async def test_api_lists_presets_with_paths(store):
    payload = await tasks_router.list_smart_tasks()

    assert payload["tasks"]
    assert payload["apps"]
    assert payload["editable_path"].endswith(store_module.STORE_FILENAME)


@pytest.mark.asyncio
async def test_api_creates_updates_and_deletes(store):
    created = await tasks_router.create_smart_task(_new_task(id="api_task"))
    assert created["id"] == "api_task"

    updated = await tasks_router.update_smart_task("api_task", _new_task(id="api_task", tag="改过"))
    assert updated["tag"] == "改过"

    assert await tasks_router.delete_smart_task("api_task") == {
        "status": "deleted",
        "id": "api_task",
    }


@pytest.mark.asyncio
async def test_api_maps_unknown_ids_to_404(store):
    with pytest.raises(HTTPException) as exc_info:
        await tasks_router.delete_smart_task("missing_id")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_api_maps_invalid_payloads_to_400(store):
    with pytest.raises(HTTPException) as exc_info:
        await tasks_router.create_smart_task(_new_task(profile="turbo"))

    assert exc_info.value.status_code == 400
