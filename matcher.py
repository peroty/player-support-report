from __future__ import annotations

import re
from difflib import SequenceMatcher


def normalize_line(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip().lower())
    text = re.sub(r"[^a-z0-9\s]", "", text)
    return text


def similarity_score(left: str, right: str) -> int:
    try:
        from rapidfuzz import fuzz

        return int(fuzz.token_set_ratio(left, right))
    except ModuleNotFoundError:
        return int(SequenceMatcher(None, left, right).ratio() * 100)
