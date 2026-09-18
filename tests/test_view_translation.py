from pathlib import Path

from scripts.generate_native_baseline import load_schema

SCHEMA = Path("/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json")


def translated_views() -> dict[str, str]:
    definitions, _, _ = load_schema(SCHEMA, include_views=True)
    return {
        str(item["name"]): str((item.get("options") or {}).get("query") or "")
        for item in definitions
        if item.get("type") == "view"
    }


def test_mechanical_view_translations_are_applied() -> None:
    views = translated_views()
    assert 'article."trackStock"' in views["buma_article_list_view"]
    assert "COALESCE(stock.stockage, 0)" not in views["buma_article_list_view"]
    assert "stock.stockage = stockage.id" in views["buma_article_list_view"]
    assert "a.id,\n  a.id" not in views["buma_article_with_no_stock_list_view"]
    assert "GREATEST(" in views["buma_sim_list_view"]
    assert 'COALESCE(asn."isNafama", false)' in views["buma_customer_mobile_list_view"]
