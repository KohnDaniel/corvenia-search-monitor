"""Query -> topic cluster, from the hand-edited keyword list in config/clusters.json.

Classification happens at read time, so editing the list re-classifies all history automatically
(spec: "recompute history when it changes"). First matching cluster wins, in file order.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "config" / "clusters.json"
UNCLASSIFIED = "unclassified"


@functools.lru_cache(maxsize=1)
def _load() -> tuple[str, list[dict], list[tuple[str, list[re.Pattern]]]]:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    compiled = [(c["id"], [re.compile(p) for p in c["patterns"]]) for c in cfg["clusters"]]
    return cfg.get("version", "?"), cfg["clusters"], compiled


@functools.lru_cache(maxsize=20000)
def classify(query: str) -> str:
    q = (query or "").lower().strip()
    for cid, patterns in _load()[2]:
        if any(p.search(q) for p in patterns):
            return cid
    return UNCLASSIFIED


def version() -> str:
    return _load()[0]


def clusters() -> list[dict]:
    """[{id, label, priority}] in file order, plus unclassified."""
    return [{"id": c["id"], "label": c["label"], "priority": c.get("priority", False)} for c in _load()[1]] + \
           [{"id": UNCLASSIFIED, "label": "Unclassified", "priority": False}]


def label(cid: str) -> str:
    return next((c["label"] for c in clusters() if c["id"] == cid), cid)


def priority_ids() -> set[str]:
    return {c["id"] for c in clusters() if c["priority"]}
