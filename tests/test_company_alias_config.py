"""config/settings.yaml's company_aliases list is data the resolver trusts
completely, so the ways it can silently be wrong are worth pinning.

The one that actually bit: `{symbol: ON, company_name: onsemi}` for onsemi.
YAML 1.1 reads a bare ON as the boolean true, so the entry arrived as
`symbol: True`, and every 股癌 mention of onsemi resolved to the company
"TRUE". Nothing failed — there was just a company called TRUE in the
dashboard, and one more in the rows pushed to X Stock Tracker.
"""
import pytest
import yaml

from app.runtime_config import CONFIG_PATH, load_runtime_config
from app.services.company_names import fuzzy_key, normalize_company_text

ALIASES = load_runtime_config().get("company_aliases", [])


def test_there_are_aliases_to_check():
    assert ALIASES, "company_aliases is empty; the rest of this file would vacuously pass"


@pytest.mark.parametrize("entry", ALIASES, ids=lambda e: str(e.get("company_name", "?")))
def test_symbol_and_name_survive_yaml_as_strings(entry):
    # bool is the failure mode: YAML 1.1 turns ON/OFF/YES/NO/Y/N into booleans.
    assert isinstance(entry.get("symbol"), str), f"symbol must be quoted in YAML: {entry!r}"
    assert isinstance(entry.get("company_name"), str), f"company_name must be a string: {entry!r}"
    assert entry["symbol"].strip(), "symbol must not be blank"


@pytest.mark.parametrize("entry", ALIASES, ids=lambda e: str(e.get("company_name", "?")))
def test_every_alias_is_a_string(entry):
    for alias in entry.get("aliases", []) or []:
        assert isinstance(alias, str), f"alias {alias!r} of {entry.get('symbol')!r} must be quoted in YAML"


def test_raw_yaml_has_no_boolean_shaped_symbols():
    """Catches the trap before it reaches the resolver, naming the fix.

    Read from the raw file rather than the parsed config so the assertion
    message can point at what to quote.
    """
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    offenders = [
        entry for entry in raw.get("company_aliases", [])
        if isinstance(entry.get("symbol"), bool)
    ]
    assert not offenders, (
        "YAML 1.1 parsed these symbols as booleans; wrap them in quotes "
        f'(symbol: "ON"): {offenders}'
    )


def test_no_two_entries_claim_the_same_symbol():
    symbols = [entry["symbol"].strip().upper() for entry in ALIASES if isinstance(entry.get("symbol"), str)]
    duplicates = {symbol for symbol in symbols if symbols.count(symbol) > 1}
    assert not duplicates, f"duplicate seed entries would shadow each other: {sorted(duplicates)}"


# --- the normalization these aliases work alongside -------------------------

@pytest.mark.parametrize("registered,spoken", [
    ("Tesla, Inc.", "Tesla"),
    ("INTEL CORP", "Intel"),
    ("Salesforce, Inc.", "salesforce"),
    ("CrowdStrike Holdings, Inc.", "CrowdStrike"),
    ("Snowflake Inc.", "SNOWFLAKE"),
    ("TEXAS INSTRUMENTS INC", "Texas Instruments"),
    ("Cloudflare, Inc.", "cloudflare"),
    ("ServiceNow, Inc.", "ServiceNow"),
])
def test_registered_name_and_spoken_name_share_a_key(registered, spoken):
    """The masters store registered names; 股癌 says the short one. Without a
    shared key each pair became two companies with the mentions split."""
    assert fuzzy_key(registered) == fuzzy_key(spoken) != ""


@pytest.mark.parametrize("a,b", [
    ("Palantir Technologies Inc.", "Palantir"),      # extra distinguishing word
    ("VISHAY INTERTECHNOLOGY INC", "Vishay"),        # and VPG is also "Vishay ..."
    ("ON SEMICONDUCTOR CORP", "onsemi"),             # abbreviation, not a suffix
])
def test_names_needing_a_human_decision_do_not_merge_on_their_own(a, b):
    """Only generic corporate suffixes are dropped. Collapsing these would
    need a rule that also merges genuinely different companies, so they are
    left to the curated alias list instead."""
    assert fuzzy_key(a) != fuzzy_key(b)


def test_generic_suffixes_alone_never_become_a_key():
    # "Co", "Ltd" and friends would otherwise collide across thousands of rows.
    assert fuzzy_key("Co., Ltd.") == ""
    assert normalize_company_text("Holdings Inc.") == ""
