"""Turn free-form user queries into safe FTS5 MATCH expressions."""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r'"[^"]*"|\S+')


def _tokens(user_query: str) -> list[str]:
    out: list[str] = []
    for raw in _TOKEN_RE.findall(user_query or ""):
        tok = raw.strip('"').strip()
        if not tok:
            continue
        # FTS5 phrase: double any embedded quotes, wrap in quotes.
        out.append('"' + tok.replace('"', '""') + '"')
    return out


def fts_query(user_query: str, *, mode: str = "and") -> str:
    """`C++ pointers` → `"C++" "pointers"` (AND) or `"C++" OR "pointers"`."""
    toks = _tokens(user_query)
    if not toks:
        return ""
    if mode == "or":
        return " OR ".join(toks)
    return " ".join(toks)
