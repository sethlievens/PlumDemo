"""
Loads artifacts/frontier.parquet (the backfill-calibration buffer sweep
behind docs/FINDINGS.md's waste%/top-up% analysis) into dbo.frontier_curve
so Power BI can read it over the same SQL connection as everything else.

Adds days_of_stock = buffer x that family's cadence_days -- a physical
quantity ("days of chicken in the case") instead of the abstract buffer
multiplier, meant to be the chart's X axis.

NOTE: frontier.parquet's columns are family/buffer/waste_pct/days_of_supply/
emergency_rate -- there is no waste_cost/stockout_pct/lost_revenue in this
artifact (that would be a $ view of the FORWARD-SIM par-level scenarios, a
different analysis; this is the backfill buffer calibration sweep). Loaded
as-is rather than inventing columns that aren't in the source data.

Only ~72 rows -- a single executemany, no BULK INSERT/CSV dance needed.
"""

from pathlib import Path

import pandas as pd
import pyodbc

FRONTIER_PARQUET = Path("artifacts/frontier.parquet")
SCHEMA_SQL = "sql/06_create_frontier_curve.sql"


def sa_password():
    line = [l for l in open(".env") if l.startswith("SA_PASSWORD=")][0]
    return line.split("=", 1)[1].strip()


def get_conn():
    pw = sa_password()
    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER=localhost,1433;"
        f"UID=sa;PWD={pw};DATABASE=PlumDemo;TrustServerCertificate=yes"
    )


def main():
    conn = get_conn()
    cur = conn.cursor()

    with open(SCHEMA_SQL) as f:
        for batch in f.read().split("GO"):
            batch = batch.strip()
            if batch:
                cur.execute(batch)
    conn.commit()

    frontier = pd.read_parquet(FRONTIER_PARQUET)

    cadence_by_family = dict(
        cur.execute("SELECT family, MIN(cadence_days) FROM dbo.dim_item GROUP BY family").fetchall()
    )
    assert set(frontier["family"].unique()) <= set(cadence_by_family), (
        "frontier.parquet has a family not present in dim_item -- cadence_days lookup would silently produce NULLs"
    )

    frontier["days_of_stock"] = frontier["buffer"] * frontier["family"].map(cadence_by_family)

    cur.fast_executemany = True
    cur.executemany(
        """
        INSERT INTO dbo.frontier_curve
            (family, buffer, waste_pct, days_of_supply, emergency_rate, days_of_stock)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        frontier[["family", "buffer", "waste_pct", "days_of_supply", "emergency_rate", "days_of_stock"]].values.tolist(),
    )
    conn.commit()

    # ---------------------------------------------------------------- gate
    row_count = cur.execute("SELECT COUNT(*) FROM dbo.frontier_curve").fetchone()[0]
    null_count = cur.execute(
        "SELECT COUNT(*) FROM dbo.frontier_curve WHERE days_of_stock IS NULL"
    ).fetchone()[0]

    print("--- validation gate ---")
    print(f"{'PASS' if row_count == len(frontier) else 'FAIL'}: row_count {row_count} == source rows {len(frontier)}")
    print(f"{'PASS' if null_count == 0 else 'FAIL'}: null days_of_stock == 0 (got {null_count})")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
