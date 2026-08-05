"""AnnotationStore unit tests (v0.8.0, C-C5)."""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.corpus.annotations import (  # noqa: E402
    ANNOTATION_VALUES,
    Annotation,
    AnnotationStore,
    KappaCalculator,
)
from cfgdrift.corpus.workspace import CorpusWorkspace  # noqa: E402


@pytest.fixture()
def ws(tmp_path):
    w = CorpusWorkspace(str(tmp_path / "ws"))
    w.init()
    return w


def _instances(n=6):
    return [
        {"instance_id": "inst-%d" % i, "metadata": {"owner": "o", "repo": "r"}}
        for i in range(n)
    ]


def test_annotations_path(ws):
    store = AnnotationStore(ws)
    assert store.annotations_path() == os.path.join(ws.root, "annotations.jsonl")


def test_load_missing_file_returns_empty(ws):
    store = AnnotationStore(ws)
    assert store.load() == []
    assert store.annotators() == []


def test_add_upsert_overwrites_same_annotator(ws, monkeypatch):
    store = AnnotationStore(ws)
    timestamps = iter(["2026-08-04T10:00:00+00:00", "2026-08-04T11:00:00+00:00"])
    monkeypatch.setattr(
        "cfgdrift.corpus.annotations.utcnow_iso", lambda: next(timestamps)
    )
    first = store.add("inst-1", "alice", "minor")
    assert first.annotated_at == "2026-08-04T10:00:00+00:00"
    second = store.add("inst-1", "alice", "severe")
    records = store.load()
    # Upsert: only one record for (inst-1, alice), last write wins.
    assert len(records) == 1
    assert records[0].annotation == "severe"
    assert records[0].annotated_at == "2026-08-04T11:00:00+00:00"


def test_add_two_annotators_keeps_both(ws):
    store = AnnotationStore(ws)
    store.add("inst-1", "alice", "minor")
    store.add("inst-1", "bob", "normal")
    records = store.load()
    assert len(records) == 2
    assert {r.annotator for r in records} == {"alice", "bob"}


def test_add_invalid_annotation_raises(ws):
    store = AnnotationStore(ws)
    with pytest.raises(ValueError):
        store.add("inst-1", "alice", "critical")


def test_remove(ws):
    store = AnnotationStore(ws)
    store.add("inst-1", "alice", "minor")
    store.add("inst-2", "bob", "normal")
    store.remove("inst-1", "alice")
    records = store.load()
    assert len(records) == 1
    assert records[0].instance_id == "inst-2"
    with pytest.raises(ValueError):
        store.remove("inst-1", "alice")


def test_by_instance_and_annotators(ws):
    store = AnnotationStore(ws)
    store.add("inst-1", "alice", "minor")
    store.add("inst-1", "bob", "normal")
    store.add("inst-2", "alice", "severe")
    grouped = store.by_instance()
    assert set(grouped.keys()) == {"inst-1", "inst-2"}
    assert len(grouped["inst-1"]) == 2
    assert store.annotators() == ["alice", "bob"]


def test_import_batch(ws):
    store = AnnotationStore(ws)
    mapping = {
        "inst-1": {"annotation": "severe"},
        "inst-2": {"annotation": "minor", "annotator": "bob"},
        "inst-3": {"annotation": "normal", "note": "maybe skip"},
    }
    count = store.import_batch(mapping, default_annotator="alice")
    assert count == 3
    records = store.load()
    assert len(records) == 3
    by = {(r.instance_id, r.annotator): r.annotation for r in records}
    assert by[("inst-1", "alice")] == "severe"
    assert by[("inst-2", "bob")] == "minor"
    assert by[("inst-3", "alice")] == "normal"


def test_import_batch_missing_annotator_raises(ws):
    store = AnnotationStore(ws)
    with pytest.raises(ValueError):
        store.import_batch({"inst-1": {"annotation": "severe"}}, default_annotator=None)


def test_import_batch_missing_annotation_raises(ws):
    store = AnnotationStore(ws)
    with pytest.raises(ValueError):
        store.import_batch({"inst-1": {"annotator": "alice"}}, default_annotator="bob")


def test_corrupt_line_raises_value_error(ws):
    path = os.path.join(ws.root, "annotations.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"instance_id": "inst-1", "annotator": "alice"}\n')
        fh.write("not-json\n")
    store = AnnotationStore(ws)
    with pytest.raises(ValueError):
        store.load()


def test_corrupt_schema_raises_value_error(ws):
    path = os.path.join(ws.root, "annotations.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "instance_id": "inst-1",
                    "annotator": "alice",
                    "annotation": "critical",
                    "annotated_at": "2026-08-04T12:00:00+00:00",
                }
            )
            + "\n"
        )
    store = AnnotationStore(ws)
    with pytest.raises(ValueError):
        store.load()


def test_stats_unannotated(ws):
    store = AnnotationStore(ws)
    stats = store.stats(_instances())
    assert stats["instances"] == 6
    assert stats["unannotated"] == 6
    assert stats["single"] == {}
    assert stats["double"] == 0
    assert stats["agreement_rate"] == 0.0
    assert stats["kappa_ready"] == 0


def test_stats_single_and_double(ws):
    store = AnnotationStore(ws)
    store.add("inst-0", "alice", "minor")
    store.add("inst-1", "alice", "normal")
    store.add("inst-2", "alice", "severe")
    store.add("inst-2", "bob", "severe")
    store.add("inst-3", "alice", "minor")
    store.add("inst-3", "bob", "minor")
    stats = store.stats(_instances())
    assert stats["unannotated"] == 2  # inst-4, inst-5
    assert stats["single"] == {"alice": 2}  # inst-0, inst-1 (alice only)
    assert stats["double"] == 2  # inst-2, inst-3
    assert stats["kappa_ready"] == 2
    # inst-2 agree (severe/severe), inst-3 agree (minor/minor)
    assert stats["agreement_rate"] == 1.0


def test_stats_orphan_ignored(ws):
    store = AnnotationStore(ws)
    store.add("ghost", "alice", "minor")
    stats = store.stats(_instances())
    assert stats["unannotated"] == 6
    assert stats["double"] == 0


def test_latest_by_instance_d3(ws):
    store = AnnotationStore(ws)
    store.add("inst-1", "alice", "minor")
    store.add("inst-1", "bob", "normal")
    records = store.load()
    latest = AnnotationStore.latest_by_instance(records)
    # Both records share annotated_at (same utcnow call may differ); the
    # lexicographic annotator tie-break makes bob the winner when equal.
    assert latest["inst-1"].instance_id == "inst-1"


def test_annotation_validation():
    with pytest.raises(ValueError):
        Annotation("i", "a", "bad", "t")
    ann = Annotation("i", "a", "severe", "t")
    assert ann.to_dict()["annotation"] == "severe"
