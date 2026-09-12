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


def _grantee(role: str) -> str:
    """Render one role as a GRANT target.

    PUBLIC is a keyword, not a role name — quoting it grants to nobody. Every
    other name is a quoted identifier, and a quoted identifier escapes an
    embedded double quote by doubling it. Without that, a role name containing
    one closes the quoting early and the rest of the name becomes SQL.
    """
    name = role.strip()
    if name.upper() == "PUBLIC":
        return "PUBLIC"
    return '"{}"'.format(name.replace('"', '""'))


def statements(reader_roles: list[str]) -> list[str]:
    """Every statement needed to (re)create the view, in order.

    `reader_roles` get SELECT on the view — the account the consumer connects
    as. A superuser needs no grant but is harmless to include.

    Dropping the view also drops its grants, so a caller that re-runs this must
    pass every role that could already read it, not just the ones it wants to
    add; scripts/create_unified_view.py reads the existing grantees back out of
    the catalog for exactly that reason. Getting it wrong silently cuts off a
    consumer that connects as its own account.
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
    # Case-sensitively: a quoted identifier is case-sensitive in PostgreSQL, so
    # "Reader" and "reader" are two different roles and folding them together
    # silently drops a grant. Only PUBLIC is matched case-insensitively, because
    # there it is a keyword rather than a name.
    seen = set()
    for role in reader_roles:
        if not role or not role.strip():
            continue
        key = "PUBLIC" if role.strip().upper() == "PUBLIC" else role.strip()
        if key not in seen:
            seen.add(key)
            sql.append(f"GRANT SELECT ON {VIEW_NAME} TO {_grantee(role)}")
    return sql


def full_sql(reader_roles: list[str]) -> str:
    """The same statements as one pasteable script for psql.

    Wrapped in a transaction, because the first statement drops the view: a
    paste that fails halfway — a typo'd role, a permissions error on the GRANT
    — would otherwise leave consumers with no view at all until someone
    noticed. scripts/create_unified_view.py gets this from engine.begin(); a
    human running the printed SQL deserves the same protection.
    """
    body = ";\n\n".join(statements(reader_roles))
    return f"BEGIN;\n\n{body};\n\nCOMMIT;\n"
