"""cfgdrift v0.11.0 severity-distribution linkage tests (P0-4).

The linkage is pure frontend (D5): a click on a ``#severitySvg [data-sev]``
slice sets ``timelineState.severity`` (same slice again cancels) and calls
``switchView("timeline")``, reusing the existing ``/api/scans?severity=``
filter.  With no JS test runner in this environment these are static smoke
assertions over ``app.js`` (design D6): the ``data-sev`` attribute, the
document-level event delegation, the toggle/cancel logic and the
``switchView("timeline")`` call must all be present and consistent.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

_STATIC_JS = os.path.join(ROOT, "src", "cfgdrift", "web", "static", "app.js")


def _js() -> str:
    with open(_STATIC_JS, encoding="utf-8") as fh:
        return fh.read()


class TestPieSliceData:
    def test_slices_carry_data_sev_and_pointer(self):
        js = _js()
        # renderSvgPie builds each slice as
        # '<path data-sev="' + k + '" style="cursor:pointer" d=...'
        assert "'<path data-sev=\"' + k + '\" style=\"cursor:pointer\" d=\"M'" in js
        # every severity slice is annotated via the dynamic k loop
        assert "data-sev" in js
        assert 'id="severitySvg"' in js

    def test_empty_state_has_no_paths(self):
        js = _js()
        # the empty state renders a <p> (no path), so no click target
        assert '暂无数据' in js


class TestEventDelegation:
    def test_document_delegation_wired(self):
        js = _js()
        assert 'document.addEventListener("click"' in js
        assert 'closest("#severitySvg [data-sev]")' in js
        # the delegation branch is inside the shared document listener,
        # survives re-renders, and returns before the snippet handler.
        idx_slice = js.index("#severitySvg [data-sev]")
        idx_link = js.index(".line-link")
        assert idx_slice < idx_link

    def test_toggle_cancel_logic(self):
        js = _js()
        assert 'timelineState.severity = (timelineState.severity === sev ? "" : sev);' in js
        assert "timelineState.page = 0;" in js

    def test_switches_to_timeline(self):
        js = _js()
        assert 'switchView("timeline")' in js

    def test_timeline_severity_drives_filter(self):
        js = _js()
        # renderTimeline sends the severity param and the dropdown reflects it
        assert 'params.set("severity", timelineState.severity)' in js
        assert "tlSev" in js


class TestPieRenderingUnchanged:
    def test_svg_container_preserved(self):
        js = _js()
        assert 'id="severitySvg"' in js
        # no new styling/paths on the pie itself beyond data-sev + cursor
        assert 'renderSvgPie' in js
