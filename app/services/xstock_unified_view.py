"""A SQL view that hands consumers exactly one row per company.

`x_company_views` stores one row per source — Gooaye's 股癌 view, X Stock
Tracker's analyst-community view, and the fused `merged` row that
app/services/merged_views.py writes on top of them. That split is right for
the dashboards, which label each card by source, and wrong for a consumer
that looks a company up by name and shows every match: the LINE bot's
`/觀點 MU` returned two cards for one company.

This view resolves that at read time, preferring the merged row and falling
back to a single-source row for any company the merge has not reached yet, so
the view never goes blank between runs.

Nothing is written and no table is altered — both projects keep working
exactly as before; this only adds another way to read the same rows. The
column names deliberately match the ones the LINE bot already probes for
(symbol / company_name / thesis / strengths_json / weaknesses_json /
evidence_urls_json / stance / mention_count / updated_at), so pointing a
consumer at it is a configuration change rather than a code change.
"""
from __future__ import annotations

VIEW_NAME = "public.company_views_unified"
SOURCE_TABLE = "public.x_company_views"

_VIEW_SQL = f"""
CREATE OR REPLACE VIEW {VIEW_NAME} AS
SELECT DISTINCT ON (upper(btrim(symbol)))
    upper(btrim(symbol)) AS symbol,
    company_name,
    stance,
    confidence,
    thesis,
    strengths_json,
    weaknesses_json,
    evidence_urls_json,
    mention_count,
    updated_at,
    source
FROM {SOURCE_TABLE}
ORDER BY upper(btrim(symbol)),
         CASE source WHEN 'merged' THEN 0 WHEN 'gooaye' THEN 1 ELSE 2 END,
         updated_at DESC
"""


def statements(reader_roles: list[str]) -> list[str]:
    """Every statement needed to (re)create the view, in order.

    `reader_roles` get SELECT on the view — the account the consumer connects
    as. A superuser needs no grant but is harmless to include.
    """
    # CREATE OR REPLACE cannot remove a column from an existing view, so a
    # changed definition has to drop first. Callers run these in one
    # transaction, so readers never see the view missing.
    sql = [
        f"DROP VIEW IF EXISTS {VIEW_NAME}",
        # Orphaned by the current definition, which no longer parses JSON.
        "DROP FUNCTION IF EXISTS public.gooaye_json_array(text)",
        _VIEW_SQL.strip(),
    ]
    for role in reader_roles:
        if role and role.strip():
            sql.append(f'GRANT SELECT ON {VIEW_NAME} TO "{role.strip()}"')
    return sql


def full_sql(reader_roles: list[str]) -> str:
    """The same statements as one pasteable script for psql."""
    return ";\n\n".join(statements(reader_roles)) + ";\n"
