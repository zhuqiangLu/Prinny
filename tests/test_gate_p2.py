"""AGENTIC_PLAN Phase 2 — the gate.

Pins each provenance type's grounding power and the structural claim_type floor.
Uses a controllable effective_stamp so the gate's logic is tested in isolation from
storage (the resolver itself is covered by test_provenance_p1).
"""
from __future__ import annotations

import pytest

import app.wiki as wiki


@pytest.fixture
def ctx():
    return {
        "slug": "x",
        "valid_notes": {"1", "2"},
        "valid_thoughts": {"T1"},
        "valid_papers": {"1", "2"},
        "valid_highlights": {10, 11},
        "hl_to_paper": {10: "1", 11: "2"},
    }


@pytest.fixture
def stamps(monkeypatch):
    """Map specific refs to stamps; default (seed, human)."""
    table = {}

    def fake(ref, slug=None):
        return table.get((ref.get("type"), str(ref.get("id"))), ("seed", "human"))

    monkeypatch.setattr("app.provenance.effective_stamp", fake)
    return table


# --- attributed branch: must cite the source (paper or highlight) ---------------
def test_attributed_with_paper_accepts(ctx, stamps):
    assert wiki.gate({"text": "P reports R", "papers": ["1"]}, ctx)[0] == wiki.ACCEPT


def test_attributed_with_highlight_accepts(ctx, stamps):
    out, clean = wiki.gate({"text": "P reports R", "highlights": [10]}, ctx)
    assert out == wiki.ACCEPT and clean["claim_type"] == "attributed"


def test_attributed_note_only_rejects(ctx, stamps):
    # a note is the user's interpretation, not the paper's words -> can't attribute
    assert wiki.gate({"text": "P reports R", "notes": ["1"]}, ctx)[0] == wiki.REJECT


def test_empty_or_unsupported_rejects(ctx, stamps):
    assert wiki.gate({"text": "", "papers": ["1"]}, ctx)[0] == wiki.REJECT
    assert wiki.gate({"text": "x", "papers": ["GHOST"]}, ctx)[0] == wiki.REJECT


# --- structural floor: agent can only tighten ----------------------------------
def test_two_papers_forces_synthesis_even_if_labeled_attributed(ctx, stamps):
    # cites 2 papers => structural synthesis; agent label can't relax it to attributed
    out, clean = wiki.gate(
        {"text": "1 and 2 share F", "claim_type": "attributed", "papers": ["1", "2"]}, ctx)
    assert clean["claim_type"] == "synthesis"
    assert out == wiki.DEMOTE  # no human reasoning cited -> open question


def test_agent_can_tighten_attributed_to_synthesis(ctx, stamps):
    # single paper => structural attributed; agent labels synthesis -> stricter wins
    out, clean = wiki.gate(
        {"text": "my read of 1", "claim_type": "synthesis", "papers": ["1"]}, ctx)
    assert clean["claim_type"] == "synthesis"
    assert out == wiki.DEMOTE


# --- synthesis branch: needs (reasoning, human) to assert, else demote ----------
def test_synthesis_with_reasoning_thought_asserts(ctx, stamps):
    stamps[("thought", "T1")] = ("reasoning", "human")
    out, _ = wiki.gate(
        {"text": "X and Y share F", "claim_type": "synthesis",
         "papers": ["1", "2"], "thoughts": ["T1"]}, ctx)
    assert out == wiki.ASSERT


def test_synthesis_with_reasoning_note_asserts(ctx, stamps):
    stamps[("note", "1")] = ("reasoning", "human")
    out, _ = wiki.gate(
        {"text": "X and Y share F", "papers": ["1", "2"], "notes": ["1"]}, ctx)
    assert out == wiki.ASSERT


def test_synthesis_with_only_seed_thought_demotes(ctx, stamps):
    # a seed thought is an attention signal, not reasoning -> demote to open question
    out, _ = wiki.gate(
        {"text": "X and Y share F", "papers": ["1", "2"], "thoughts": ["T1"]}, ctx)
    assert out == wiki.DEMOTE


def test_invalid_highlight_id_is_dropped(ctx, stamps):
    out, clean = wiki.gate({"text": "x", "highlights": ["not-an-int", 99]}, ctx)
    assert clean["highlights"] == []  # non-int and unknown id both dropped
    assert out == wiki.REJECT
