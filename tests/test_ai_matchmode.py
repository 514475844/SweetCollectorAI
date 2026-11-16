"""Unit tests for the OR/AND AI match-mode toggle.

Whitelist (theme words):  "any" = keep if ANY word scores >= threshold (OR)
                          "all" = keep only if ALL words score >= threshold (AND)
Blacklist (exclude words): "any" = drop if ANY word scores >= threshold (OR)
                           "all" = drop only if ALL words score >= threshold (AND)
"""
from __future__ import annotations

from media_resource_manager.engine import Engine
from media_resource_manager.models import ImageRecord, TaskConfig


class _FakeClf:
    def __init__(self, pairs):
        self._pairs = pairs

    def classify(self, path):
        return self._pairs


def _theme_engine(pairs):
    eng = Engine(None, None)
    eng._clf = _FakeClf(pairs)
    eng._excl_clf = None
    return eng


def _excl_engine(excl_pairs):
    eng = Engine(None, None)
    eng._clf = None
    eng._excl_clf = _FakeClf(excl_pairs)
    return eng


# ---------------- whitelist (theme) ----------------
def test_apply_ai_or_keeps_single_match():
    eng = _theme_engine([("cat", 0.8), ("dog", 0.3)])
    rec = ImageRecord(id=None, source_url="u", task_id=1)
    t = TaskConfig(id=None, name="t", adapter_name="s",
                   ai_labels=["cat", "dog"], ai_threshold=0.5,
                   ai_match_mode="any")
    ok, _ = eng._apply_ai(t, rec, "p.jpg")
    assert ok == "keep"
    assert "cat" in rec.tags and "dog" not in rec.tags


def test_apply_ai_and_requires_all():
    eng = _theme_engine([("cat", 0.8), ("dog", 0.3)])
    rec = ImageRecord(id=None, source_url="u", task_id=1)
    t = TaskConfig(id=None, name="t", adapter_name="s",
                   ai_labels=["cat", "dog"], ai_threshold=0.5,
                   ai_match_mode="all")
    ok, _ = eng._apply_ai(t, rec, "p.jpg")
    assert ok == "drop"  # dog below threshold -> not all matched


def test_apply_ai_and_all_pass():
    eng = _theme_engine([("cat", 0.8), ("dog", 0.7)])
    rec = ImageRecord(id=None, source_url="u", task_id=1)
    t = TaskConfig(id=None, name="t", adapter_name="s",
                   ai_labels=["cat", "dog"], ai_threshold=0.5,
                   ai_match_mode="all")
    ok, _ = eng._apply_ai(t, rec, "p.jpg")
    assert ok == "keep"
    assert "cat" in rec.tags and "dog" in rec.tags


# ---------------- blacklist (exclude) ----------------
def test_excl_or_drops_single():
    eng = _excl_engine([("nude", 0.8)])
    t = TaskConfig(id=None, name="t", adapter_name="s",
                   ai_blacklist=["nude"], ai_threshold=0.5,
                   ai_exclude_match_mode="any")
    drop, _ = eng._ai_excluded(t, "p.jpg")
    assert drop == "drop"


def test_excl_and_requires_all():
    eng = _excl_engine([("nude", 0.8), ("violence", 0.3)])
    t = TaskConfig(id=None, name="t", adapter_name="s",
                   ai_blacklist=["nude", "violence"], ai_threshold=0.5,
                   ai_exclude_match_mode="all")
    drop, _ = eng._ai_excluded(t, "p.jpg")
    assert drop == "pass"  # violence below threshold


def test_excl_and_all_pass():
    eng = _excl_engine([("nude", 0.8), ("violence", 0.7)])
    t = TaskConfig(id=None, name="t", adapter_name="s",
                   ai_blacklist=["nude", "violence"], ai_threshold=0.5,
                   ai_exclude_match_mode="all")
    drop, _ = eng._ai_excluded(t, "p.jpg")
    assert drop == "drop"
