"""Cheap classifier: did a chat turn produce something wiki-worthy? (Phase 6).

Runs after an assistant reply. If it flags an update, the chat panel shows a
"Draft edit" chip; clicking it routes the turn into the standard proposed-edits
review queue (never auto-applied). The guardrail (must cite a note/thought/paper)
is enforced where the proposal is created, in wiki.proposal_from_chat.
"""

from __future__ import annotations

import json
import logging

from . import llm
from .wiki import SECTIONS

logger = logging.getLogger("paper_agent.suggest")


def classify(user_text: str, assistant_text: str) -> dict:
    """Return {"update": bool, "section": str|None}. Best-effort; never raises."""
    prompt = (
        "A researcher is chatting about their paper collection. Decide if the "
        "assistant's last reply contains a correction, new insight, or claim that "
        "should update their wiki. Wiki sections: "
        f"{list(SECTIONS)}.\n\n"
        f"USER: {user_text}\n\nASSISTANT: {assistant_text}\n\n"
        'Respond with JSON only: {"update": true|false, "section": "<one section or null>"}'
    )
    try:
        resp = llm.complete(
            [
                {"role": "system", "content": "You output only valid JSON."},
                {"role": "user", "content": prompt},
            ]
        )
        data = json.loads(resp[resp.find("{") : resp.rfind("}") + 1])
        section = data.get("section")
        if section not in SECTIONS:
            section = "synthesis"
        return {"update": bool(data.get("update")), "section": section}
    except Exception as exc:  # noqa: BLE001 - classification is best-effort
        logger.warning("wiki classify failed: %s", exc)
        return {"update": False, "section": None}
