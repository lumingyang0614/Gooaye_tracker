"""The manual import must synthesize before publishing and refresh consumers."""
import asyncio

from app import main


def test_import_orders_synthesis_push_and_merge_with_resolved_symbols(monkeypatch):
    calls = []
    resolved = ["2330.TW", "台積電"]
    monkeypatch.setattr(main, "postgres_health", lambda: True)

    async def rebuild(selected_symbols, use_llm):
        calls.append(("synthesize", selected_symbols, use_llm))
        return {"symbols": resolved}

    def push(selected_symbols):
        calls.append(("push", selected_symbols))
        return {"pushed": 1}

    async def merge(selected_symbols):
        calls.append(("merge", selected_symbols))
        return {"merged": 1}

    monkeypatch.setattr(main, "rebuild_company_insights", rebuild)
    monkeypatch.setattr(main, "push_company_insights", push)
    monkeypatch.setattr(main, "rebuild_merged_views", merge)
    result = asyncio.run(main.push_to_xstocktracker("台積電"))
    assert calls == [("synthesize", ["台積電"], True), ("push", resolved), ("merge", resolved)]
    assert result["pushed"] == 1
    assert result["merged_views"] == {"merged": 1}


def test_failed_import_does_not_merge_old_target_data(monkeypatch):
    monkeypatch.setattr(main, "postgres_health", lambda: True)

    async def rebuild(selected_symbols, use_llm):
        return {"symbols": ["2330.TW"]}

    async def merge(selected_symbols):
        raise AssertionError("must not merge after failed import")

    monkeypatch.setattr(main, "rebuild_company_insights", rebuild)
    monkeypatch.setattr(main, "push_company_insights", lambda selected_symbols: {"error": "offline"})
    monkeypatch.setattr(main, "rebuild_merged_views", merge)
    result = asyncio.run(main.push_to_xstocktracker())
    assert result["error"] == "offline"
    assert "merged_views" not in result
