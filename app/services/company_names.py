"""Normalize company names so the same company resolves to one ticker.

The security masters store official registered names — "Tesla, Inc.",
"INTEL CORP", "Salesforce, Inc." — while 股癌 says "Tesla", "Intel",
"Salesforce". Looking those up by exact name misses every time, so the
company is stored under the bare name the model produced and ends up as a
second company alongside the ticker: a real sync run produced TESLA *and*
TSLA, INTEL *and* INTC, SALESFORCE *and* CRM — 15 such pairs out of 167
companies, each splitting that company's mention count and evidence in half.

Two keys are indexed per alias:

* the exact name, casefolded, and
* a "fuzzy" key with punctuation and generic corporate suffix words removed
  ("Tesla, Inc." -> "tesla"), which is what makes the short spoken name match.

Only *generic* suffixes are dropped — never distinguishing words. Removing
"Technologies" from "Palantir Technologies Inc." would look tempting (股癌
says "Palantir") but the same rule would collapse genuinely different
companies, so those differences belong in config/settings.yaml's alias list
instead, where a human decided them.

The same treatment is applied on both sides — when indexing the master and
when looking a name up — otherwise the two keys never meet. Callers register
aliases through `index_alias`, which feeds the caller's own ambiguity check:
any key claimed by two companies is dropped rather than guessed at, and that
guard is what makes fuzzy keys safe to add.
"""
from __future__ import annotations

import re

# Generic corporate-form words. These carry no identity: every company can
# have them and none is distinguished by them.
_CORP_SUFFIX_WORDS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
    "limited", "holdings", "holding", "group", "plc", "sa", "nv", "ag",
}

_PUNCT_PATTERN = re.compile(r"[.,()（）、'\"]+")

# Below this, a fuzzy key is more likely to collide than to help ("co", "ag").
_MIN_FUZZY_KEY_LENGTH = 4


def normalize_company_text(value: str) -> str:
    """Strip punctuation and generic corporate suffixes, casefolded.

    "Tesla, Inc."            -> "tesla"
    "TEXAS INSTRUMENTS INC"  -> "texas instruments"
    "CrowdStrike Holdings, Inc." -> "crowdstrike"
    """
    text = _PUNCT_PATTERN.sub(" ", value).casefold()
    words = [word for word in text.split() if word not in _CORP_SUFFIX_WORDS]
    return " ".join(words).strip()


def fuzzy_key(value: str) -> str:
    """The lookup key for a name, or "" when it is too short to be safe."""
    key = normalize_company_text(value)
    return key if len(key) >= _MIN_FUZZY_KEY_LENGTH else ""


def index_alias(candidates: dict[str, set[str]], alias: str, symbol: str) -> None:
    """Register `alias` for `symbol` under both its exact and fuzzy keys.

    `candidates` maps key -> the set of symbols claiming it, so the caller can
    drop anything ambiguous afterwards; adding fuzzy keys here means they go
    through exactly the same check as exact ones.
    """
    if not alias or not str(alias).strip():
        return
    exact = str(alias).strip().casefold()
    candidates.setdefault(exact, set()).add(symbol)
    key = fuzzy_key(str(alias))
    if key and key != exact:
        candidates.setdefault(key, set()).add(symbol)
