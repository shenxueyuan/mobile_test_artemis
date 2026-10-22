"""Eligibility and deterministic-path tests for the Jev fast navigation lane."""

import pytest

from artemis.agents.flash.jev_decision import (
    choose_navigation_action,
    navigation_candidates,
)
from artemis.config.agent import JevFastConfig


def _element(index, text="字词听写", **overrides):
    element = {
        "index": index,
        "text": text,
        "clickable": True,
        "enabled": True,
        "is_ocr": False,
        "bounds": (0, 0, 100, 50),
        "package": "com.example.app",
    }
    element.update(overrides)
    return element


def _cfg(**overrides):
    return JevFastConfig(enabled=True, **overrides)


def test_later_clause_danger_words_do_not_block_navigation():
    goal = "打开字词听写，输入苹果、香蕉后，点击下一步，直到完成"
    candidates = navigation_candidates(goal, [_element(1), _element(2, "设置")], _cfg())
    assert [c["index"] for c in candidates] == [1, 2]


def test_dangerous_goal_still_blocks():
    assert navigation_candidates("打开支付页面", [_element(1)], _cfg()) == []


def test_non_verb_goal_rejected():
    assert navigation_candidates("输入苹果两个字", [_element(1)], _cfg()) == []


def test_click_and_select_verbs_are_eligible():
    assert navigation_candidates("点击教材同步字词", [_element(1)], _cfg()) != []
    assert navigation_candidates("选择 【嗡】", [_element(1)], _cfg()) != []


def test_candidate_filtering():
    elements = [
        _element(1, "可用"),
        _element(2, "不可点", clickable=False),
        _element(3, "禁用", enabled=False),
        _element(4, "识别文字", is_ocr=True),
        _element(5, "下一步"),
        _element(6, "状态栏", package="com.android.systemui"),
        _element(7, ""),
        _element(8, "坏边界", bounds=(0, 0, 0, 0)),
    ]
    candidates = navigation_candidates("打开可用", elements, _cfg())
    assert [c["index"] for c in candidates] == [1]


def test_too_many_candidates_defer():
    elements = [_element(i, f"项{i}") for i in range(1, 12)]
    assert navigation_candidates("打开项1", elements, _cfg()) == []


def test_non_contiguous_index_rejected():
    elements = [_element(1, "甲"), _element(3, "乙")]
    candidates = navigation_candidates("打开甲", elements, _cfg())
    assert [c["index"] for c in candidates] == [1]


@pytest.mark.asyncio
async def test_deterministic_named_element_skips_jev():
    elements = [_element(1, "设置"), _element(2, "字词听写"), _element(3, "相册")]
    index, source, _ = await choose_navigation_action("打开字词听写", elements, _cfg())
    assert (index, source) == (2, "deterministic")


@pytest.mark.asyncio
async def test_objective_advances_past_tapped_clauses():
    goal = "打开应用列表，打开字词听写，点击教材同步字词"
    elements = [_element(1, "字词听写"), _element(2, "教材同步字词")]
    index, source, _ = await choose_navigation_action(
        goal, elements, _cfg(), recent_actions=["字词听写"]
    )
    assert (index, source) == (2, "deterministic")


@pytest.mark.asyncio
async def test_no_target_on_screen_defers_without_api():
    elements = [_element(1, "相册"), _element(2, "相机")]
    index, source, _ = await choose_navigation_action("打开字词听写", elements, _cfg())
    assert (index, source) == (None, "no_target")


@pytest.mark.asyncio
async def test_ineligible_returns_none_without_api_call():
    index, source, _ = await choose_navigation_action("输入苹果", [_element(1)], _cfg())
    assert (index, source) == (None, "ineligible")
