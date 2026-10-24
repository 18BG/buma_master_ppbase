"""Record.PublicExport behavior for the three rowid views verified on PB 0.22."""

from ppbase.models.field_multiplicity import field_is_multiple

VIEWS = frozenset(
    {
        "buma_article_with_no_stock_list_view",
        "buma_stockage_no_manager",
        "buma_stock_transaction_list_view",
    }
)
_INSTALLED = False


def source_view_row(row, schema):
    values = dict(row)
    for field in schema:
        name, kind = field["name"], field["type"]
        if values.get(name) is not None:
            continue
        if field_is_multiple(field):  # shared PPBase rule: maxSelect null is multiple
            values[name] = []
        elif kind == "bool":
            values[name] = False
        elif kind == "number":
            values[name] = 0
        elif kind != "json":
            values[name] = ""
    return values


def install_view_serialization():
    global _INSTALLED
    if _INSTALLED:
        return
    from ppbase.services import record_service

    original = record_service.build_record_response

    def build(row, collection_id, collection_name, schema, **kwargs):
        if collection_name not in VIEWS:
            return original(row, collection_id, collection_name, schema, **kwargs)
        result = original(
            source_view_row(row, schema), collection_id, collection_name, schema, **kwargs
        )
        # PB PublicExport omits zero-valued created/updated on view records.
        for field in ("created", "updated"):
            if not row.get(field):
                result.pop(field, None)
        return result

    record_service.build_record_response = build
    _INSTALLED = True
