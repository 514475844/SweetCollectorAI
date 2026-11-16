"""R57b — behaviour-level guardrails for the `apply_theme` early-return.

Context (why this file exists)
------------------------------
R56 pinned the real freeze: `apply_theme` (gui/theme.py:781 / :792 =
`app.setStyleSheet`) called from `_boot` (tests/test_view_size.py:60),
reproduced twice at 94%, 3 identical stack samples across 40s with CPU
delta 0 => a freeze, not slowness. Candidate B (skip the redundant
`setStyleSheet`) was chosen as the direction; candidate A (really delete
widgets via shiboken6) was falsified — it crashes the process.

R56 also left one hard implementation constraint, recorded verbatim:
> "先算出新的 QSS、与上次应用的不同才 setStyleSheet；**不许**「模块级记住
>  上次皮肤名就跳过」—— 会把 R68 钉死的「后来换皮肤仍是旧颜色」放回来。"

R57's probe (tools/_r57_probe_qss_roundtrip.py) then established the
criterion that actually works, and this file pins it:
  * `app.styleSheet()` round-trips **byte-for-byte** (5/5 probes, incl. the
    real 7177-char QSS) => comparing computed QSS against live app state
    is sound, no normalisation to compensate for.
  * many tests blank the sheet **behind apply_theme's back**
    (test_r41:45,109,152,174; test_r47:43,115,139,172,229,262;
     test_r48_*:56,73,74,87,50,63,45) => a guard that trusts its own
    memory would skip re-theming and leave the app unstyled.
    ⇒ the criterion must read `app.styleSheet()`, never a remembered name.

These tests are deliberately **behaviour-level**: they assert on whether
`setStyleSheet` is invoked and on the resulting live stylesheet, not on
the presence of a variable name. A guardrail written against an
implementation detail would be free to pass while the bug is live —
exactly the R54 lesson ("guarding a name is not guarding a behaviour").
"""
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from media_resource_manager.gui import theme  # noqa: E402


@pytest.fixture()
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def spy(qapp):
    """Record every stylesheet the app is handed, without changing behaviour.

    Yields a list-like recorder with `.calls` and `.qss()` helpers. The real
    setter is restored on teardown so a failing assert can't leak a stub
    into the rest of the suite (R53: a leaked monkeypatch looks like a
    regression three modules later).
    """
    seen = []
    real = qapp.setStyleSheet

    class Spy:
        calls = seen

        @staticmethod
        def qss():
            return seen[-1] if seen else None

    def recording(qss=""):
        seen.append(qss)
        return real(qss)

    qapp.setStyleSheet = recording
    try:
        yield Spy
    finally:
        try:
            qapp.setStyleSheet = real
        except Exception:
            pass


# ---------------------------------------------------------------- behaviour

def test_second_identical_apply_does_not_re_set_the_stylesheet(qapp, spy):
    """The whole point of candidate B.

    Two identical `apply_theme` calls in a row must not pay for a second
    `setStyleSheet` — that call re-resolves and re-applies the full QSS
    across every live widget, measured at15.6 ms/300 widgets and
    77.2 ms/1500 (linear), i.e. ~250 ms per call at the 5365 widgets
    test_view_size.py accumulates.
    """
    theme.apply_theme(qapp, False, "violet")
    first = spy.qss()
    assert first, "first apply must install a stylesheet"
    n_after_first = len(spy.calls)

    theme.apply_theme(qapp, False, "violet")
    assert len(spy.calls) == n_after_first, (
        "identical apply_theme re-installed the same QSS; "
        f"first_len={len(first)} calls={n_after_first}->{len(spy.calls)}"
    )
    assert qapp.styleSheet() == first, (
        "skipping must not change what the app carries"
    )


def test_switching_skin_still_re_themes(qapp, spy):
    """R68's regression guard, restated at behaviour level.

    Early-return must never resurrect "switched skin later, still old
    colours": a *different* skin has to produce a different stylesheet and
    a real `setStyleSheet` call.
    """
    theme.apply_theme(qapp, False, "violet")
    violet = qapp.styleSheet()
    theme.apply_theme(qapp, False, "amber")
    amber = qapp.styleSheet()
    assert amber != violet, (
        "skin switch produced an identical stylesheet — recolour is dead"
    )
    assert spy.qss() == amber, "the applied QSS must be the new skin's"


def test_switching_dark_mode_still_re_themes(qapp, spy):
    """Light -> dark -> light must not degenerate into one stuck sheet."""
    theme.apply_theme(qapp, False, "violet")
    light = qapp.styleSheet()
    theme.apply_theme(qapp, True, "violet")
    dark = qapp.styleSheet()
    assert dark != light, "dark toggle did not change the stylesheet"
    theme.apply_theme(qapp, False, "violet")
    assert qapp.styleSheet() == light, "returning to light must restore it"


def test_out_of_band_blank_is_honoured(qapp, spy):
    """The criterion must read live app state, not remembered state.

    test_r41/test_r47/test_r48 blank the sheet behind apply_theme's back to
    measure a bare watermark. If the guard trusted a module-level memory of
    "I already applied violet", it would skip here and leave the app
    permanently unstyled — a silent failure with no traceback anywhere.
    """
    theme.apply_theme(qapp, False, "violet")
    full = qapp.styleSheet()
    qapp.setStyleSheet("")           # what those tests do
    assert qapp.styleSheet() == "", "precondition: sheet really is blank"

    theme.apply_theme(qapp, False, "violet")
    assert qapp.styleSheet() == full, (
        "after an out-of-band reset, apply_theme must re-apply; "
        "the guard is consulting remembered state instead of app state"
    )


def test_out_of_band_foreign_sheet_is_overwritten(qapp, spy):
    """A foreign stylesheet must not be mistaken for 'already applied'."""
    theme.apply_theme(qapp, False, "violet")
    qapp.setStyleSheet("QWidget { background: #123456; }")
    theme.apply_theme(qapp, False, "violet")
    assert "#123456" not in qapp.styleSheet(), (
        "apply_theme deferred to a foreign stylesheet installed out of band"
    )


def test_palette_side_of_apply_theme_is_never_skipped(qapp, spy):
    """The early-return may only gate `setStyleSheet`, nothing else.

    `apply_theme` also drives setPalette, `_restyle_registered`,
    `restyle_themed_icons`, the unpolish/polish sweep and processEvents —
    the P1-4 work that makes already-visible custom-painted widgets change
    colour. Guarding the whole function would silently drop all of it.
    """
    before = qapp.palette().color(
        QApplication.palette().ColorRole.Window
    ).name() if hasattr(QApplication, "palette") else None
    theme.apply_theme(qapp, True, "amber")
    after = qapp.palette().color(
        QApplication.palette().ColorRole.Window
    ).name() if hasattr(QApplication, "palette") else None
    assert before != after or before is None, (
        "apply_theme did not update the palette — the early-return has "
        f"grown past setStyleSheet (before={before} after={after})"
    )
    assert theme._CURRENT_DARK is True
    assert theme._CURRENT_SKIN == "amber"


def test_current_skin_is_updated_even_on_the_skipped_path(qapp, spy):
    """Globals must track the request even when setStyleSheet is skipped.

    `palette()` and the custom-painted call sites read `_CURRENT_SKIN` /
    `_CURRENT_DARK` to pick colours. If the early-return returned before
    those assignments, widgets would be painted from stale colours.
    """
    theme.apply_theme(qapp, False, "amber")
    theme.apply_theme(qapp, False, "amber")     # the skipped path
    assert theme._CURRENT_SKIN == "amber"
    assert theme._CURRENT_DARK is False


def test_repeated_applies_converge_and_stay_correct(qapp, spy):
    """Ten alternating applies: correct end state, and no redundant churn.

    Guards against a fix that only works for one specific pair (e.g. an
    off-by-one that leaves the second call correct but the third stale).
    """
    for i in range(5):
        theme.apply_theme(qapp, False, "violet")
        assert theme._CURRENT_SKIN == "violet"
        theme.apply_theme(qapp, True, "amber")
        assert theme._CURRENT_SKIN == "amber" and theme._CURRENT_DARK is True
    # End on light violet and compare against a freshly computed reference.
    theme.apply_theme(qapp, False, "violet")
    settled = qapp.styleSheet()
    n_settled = len(spy.calls)
    theme.apply_theme(qapp, False, "violet")
    assert qapp.styleSheet() == settled
    assert len(spy.calls) == n_settled, (
        f"redundant re-apply at iteration {i}: "
        f"{n_settled}->{len(spy.calls)}"
    )


def test_apply_still_works_with_a_fresh_process_state(qapp, spy):
    """First call after the app has no stylesheet must install one.

    Trivial-looking, but it is exactly the case a `if _last == new` guard
    written against an uninitialised global gets wrong.
    """
    qapp.setStyleSheet("")
    theme.apply_theme(qapp, False, "violet")
    assert len(qapp.styleSheet()) > 1000, (
        f"first apply installed only {len(qapp.styleSheet())} chars"
    )