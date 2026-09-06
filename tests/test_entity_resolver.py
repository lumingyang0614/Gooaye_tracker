from app.services.entity_resolver import canonicalize_symbols, resolve_company_aliases


def test_canonicalizes_chinese_company_name_to_seed_ticker():
    assert canonicalize_symbols({"台積電"}) == {"2330.TW"}


def test_unknown_symbol_passes_through_uppercased():
    assert canonicalize_symbols({"unknown corp"}) == {"UNKNOWN CORP"}


def test_resolve_company_aliases_matches_english_alias():
    matches = resolve_company_aliases("大家都在聊 TSMC 的資本支出")
    assert any(company["symbol"] == "2330.TW" for company in matches)
