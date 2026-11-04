"""Optional: execute the generated validation SQL in Snowflake.

Configure under the tenant profile in secrets:

    [tenants.contoso.snowflake]
    account   = "acme-xy12345"
    user      = "PBI_VALIDATOR"
    password  = "..."            # or private_key_path / authenticator
    warehouse = "ANALYTICS_WH"
    role      = "READ_ONLY"
"""
from __future__ import annotations

from typing import Dict

import pandas as pd


def run_sql(cfg: Dict[str, str], sql: str, limit: int = 100000) -> pd.DataFrame:
    import snowflake.connector  # lazy: only needed when the owner configures Snowflake

    params = {k: v for k, v in cfg.items() if k in (
        "account", "user", "password", "warehouse", "role", "database", "schema",
        "authenticator", "private_key_file", "private_key_file_pwd")}
    with snowflake.connector.connect(**params, client_session_keep_alive=False) as conn:
        cur = conn.cursor()
        try:
            cur.execute("ALTER SESSION SET QUERY_TAG = 'pbi-compare-validation'")
            cur.execute(sql.rstrip().rstrip(";"))
            return cur.fetch_pandas_all() if hasattr(cur, "fetch_pandas_all") else \
                pd.DataFrame(cur.fetchmany(limit), columns=[c[0] for c in cur.description])
        finally:
            cur.close()
