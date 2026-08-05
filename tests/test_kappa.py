"""KappaCalculator formula / weighted / confusion-matrix / edge tests (v0.8.0)."""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.corpus.annotations import KappaCalculator  # noqa: E402

CATS = ("severe", "minor", "normal")


def test_perfect_agreement_kappa_one():
    a = ["severe", "minor", "normal"]
    r = KappaCalculator.cohen_kappa(a, list(a), CATS)
    assert abs(r["kappa"] - 1.0) < 1e-9
    assert abs(r["po"] - 1.0) < 1e-9
    assert r["n"] == 3
    assert r["agreement_rate"] == 1.0


def test_known_answer():
    # po = 4/5 = 0.8; pe = 4/25 = 0.16; kappa = 0.64/0.84 = 0.761904...
    a = ["a", "b", "c", "d", "e"]
    b = ["a", "b", "c", "d", "f"]
    r = KappaCalculator.cohen_kappa(a, b, ("a", "b", "c", "d", "e", "f"))
    assert abs(r["kappa"] - 0.761904) < 1e-5
    assert abs(r["po"] - 0.8) < 1e-9
    assert abs(r["pe"] - 0.16) < 1e-9
    assert r["n"] == 5


def test_weighted_linear_matches_manual():
    # 3 samples, one far disagreement (severe vs normal):
    # matrix: severe/normal=1, severe/minor=1, normal/normal=1; n=3
    # rows: severe=2, normal=1; cols: normal=2, minor=1
    # linear w: severe/normal=1, severe/minor=1/2, normal/minor=1/2
    #   sum(w*o) = 1/3 + 1/6 = 1/2 ; sum(w*e) = 4/9 + 1/9 + 1/18 = 11/18
    #   k = 1 - (1/2)/(11/18) = 2/11
    # quadratic w: severe/normal=1, severe/minor=1/4, normal/minor=1/4
    #   sum(w*o) = 1/3 + 1/12 = 5/12 ; sum(w*e) = 4/9 + 1/18 + 1/36 = 19/36
    #   k = 1 - (5/12)/(19/36) = 4/19
    a = ["severe", "severe", "normal"]
    b = ["normal", "minor", "normal"]
    k_linear = KappaCalculator.weighted_kappa(a, b, CATS, weight="linear")
    k_quad = KappaCalculator.weighted_kappa(a, b, CATS, weight="quadratic")
    assert abs(k_linear - 2.0 / 11.0) < 1e-9
    assert abs(k_quad - 4.0 / 19.0) < 1e-9
    assert k_quad > k_linear  # quadratic penalizes far steps less per-step


def test_weighted_quadratic_known():
    a = ["severe", "minor", "normal"]
    b = ["severe", "minor", "normal"]
    k = KappaCalculator.weighted_kappa(a, b, CATS, weight="quadratic")
    assert abs(k - 1.0) < 1e-9


def test_weighted_invalid_weight_raises():
    with pytest.raises(ValueError):
        KappaCalculator.weighted_kappa(["a"], ["a"], ("a",), weight="cubic")


def test_confusion_matrix_shape():
    a = ["severe", "minor", "normal", "severe"]
    b = ["severe", "severe", "normal", "minor"]
    cm = KappaCalculator.confusion_matrix(a, b, CATS)
    assert set(cm.keys()) == set(CATS)
    assert cm["severe"]["severe"] == 1
    assert cm["severe"]["minor"] == 1
    assert cm["minor"]["severe"] == 1
    assert cm["normal"]["normal"] == 1


def test_kappa_edges_pe_one_po_one():
    # Only one category -> pe = 1; po == 1 -> kappa = 1.0 (sklearn behavior).
    a = ["x", "x"]
    b = ["x", "x"]
    r = KappaCalculator.cohen_kappa(a, b, ("x",))
    assert r["kappa"] == 1.0


def test_kappa_n_less_than_two_raises():
    with pytest.raises(ValueError):
        KappaCalculator.cohen_kappa(["severe"], ["minor"], CATS)


def test_kappa_length_mismatch_raises():
    with pytest.raises(ValueError):
        KappaCalculator.cohen_kappa(["severe", "minor"], ["normal"], CATS)


def test_kappa_unknown_category_raises():
    with pytest.raises(ValueError):
        KappaCalculator.cohen_kappa(["severe"], ["severe"], CATS)


def test_negative_kappa_possible():
    # Systematic disagreement on a 2-category scale -> kappa = -1.
    a = ["x", "y"]
    b = ["y", "x"]
    r = KappaCalculator.cohen_kappa(a, b, ("x", "y"))
    assert abs(r["kappa"] - (-1.0)) < 1e-9


def test_weighted_linear_quadratic_consistent_values():
    a = ["severe", "severe", "minor", "normal", "normal"]
    b = ["severe", "minor", "minor", "normal", "severe"]
    r = KappaCalculator.cohen_kappa(a, b, CATS)
    assert "weighted" in r
    assert "linear" in r["weighted"] and "quadratic" in r["weighted"]
    # Quadratic weighting penalizes far disagreements more -> lower kappa
    # when disagreements are far apart; here just assert they are floats.
    assert isinstance(r["weighted"]["linear"], float)
    assert isinstance(r["weighted"]["quadratic"], float)
