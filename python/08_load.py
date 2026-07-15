"""
Bulk loads data/staging/ into the PlumDemo SQL Server database per
docs/SCHEMA.md. Runs sql/01_schema.sql first (idempotent), then builds one
CSV per table and loads it with BULK INSERT -- never row-by-row INSERTs.

SQL Server runs in a Docker container (plum-sql) with no bind mount to this
project's data/staging/ directory -- BULK INSERT reads from the SQL Server
process's own filesystem, not the host's. So this script writes CSVs to a
local folder, `docker cp`s that folder into the container once, then issues
BULK INSERT statements against the in-container path.

Dimensions load first, into "v_load_*" views (see 01_schema.sql) that expose
every column except the surrogate identity PK -- BULK INSERT only maps by
ordinal position, so this is how the auto-generated keys get skipped without
a format file. Their generated IDENTITY values are then read back and used
to translate every fact/config CSV's natural keys (store_nbr, item_nbr, ...)
to surrogate keys before those are bulk loaded.
"""

import subprocess
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyodbc

STAGING = Path("data/staging")
CSV_DIR = Path("data/staging/csv")
CONTAINER = "plum-sql"
CONTAINER_LOAD_PATH = "/tmp/plum_load"
SCHEMA_SQL = "sql/01_schema.sql"

DATE_START = "2015-01-01"
DATE_END = "2017-08-15"

VENDOR_BY_FAMILY = {
    "PRODUCE": "Andes Fresh Produce Co.",
    "DAIRY": "Sierra Dairy Direct",
    "DELI": "In-Store Deli Kitchen",
    "BREAD/BAKERY": "In-Store Bakery",
    "BEVERAGES": "Costa Beverage Distributors",
    "GROCERY": "Pacific Dry Goods Supply",
}

SCENARIOS = [
    ("Historical", "Reconstructed actuals: calibrated backfill + injected phantom events"),
    ("Baseline Forward", "Forward simulation under current ordering behavior -- not yet populated"),
    ("Recommended Par", "Forward simulation under the recommended par-level policy -- not yet populated"),
    ("Aggressive Par", "Forward simulation under an aggressive par-level policy -- not yet populated"),
    ("Tuned Par", "Forward simulation under a per-family tuned service-level policy -- not yet populated"),
]


def sa_password():
    line = [l for l in open(".env") if l.startswith("SA_PASSWORD=")][0]
    return line.split("=", 1)[1].strip()


def get_conn():
    pw = sa_password()
    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER=localhost,1433;"
        f"UID=sa;PWD={pw};DATABASE=PlumDemo;TrustServerCertificate=yes"
    )


def run_schema():
    subprocess.run(
        ["/opt/mssql-tools18/bin/sqlcmd", "-S", "localhost", "-U", "sa", "-P", sa_password(),
         "-C", "-i", SCHEMA_SQL],
        check=True, capture_output=True, text=True,
    )


def bulk_insert(cur, target, csv_name):
    """target is a table or a v_load_* view; BULK INSERT maps CSV columns to
    it by ordinal position only, in file order."""
    path = f"{CONTAINER_LOAD_PATH}/{csv_name}"
    cur.execute(f"""
        BULK INSERT dbo.{target}
        FROM '{path}'
        WITH (FORMAT = 'CSV', FIRSTROW = 2, TABLOCK)
    """)


def write_csv(df, name):
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(CSV_DIR / name, index=False, encoding="utf-8")


def sync_csv_dir_to_container():
    """docker cp into an already-existing destination nests the source dir
    one level deeper instead of replacing its contents -- always wipe the
    container path first so this is a clean copy every time, not just on
    the very first call."""
    subprocess.run(["docker", "exec", "-u", "root", CONTAINER, "rm", "-rf", CONTAINER_LOAD_PATH], check=True)
    subprocess.run(["docker", "cp", str(CSV_DIR), f"{CONTAINER}:{CONTAINER_LOAD_PATH}"], check=True)


# ---------------------------------------------------------------- dimensions

def build_dim_department(econ):
    return pd.DataFrame({"dept_name": sorted(econ["family"].unique())})


def build_dim_store(stores, subset_store_nbrs):
    s = stores.loc[stores["store_nbr"].isin(subset_store_nbrs)].copy()
    type_rank = {t: i + 1 for i, t in enumerate(sorted(s["type"].unique()))}
    s["peer_group_id"] = s["type"].map(type_rank)
    return s.rename(columns={"type": "store_type", "cluster": "cluster_id"})[
        ["store_nbr", "city", "state", "store_type", "cluster_id", "peer_group_id"]
    ]


def build_dim_item(items, econ, dept_key_by_family):
    d = econ.merge(items[["item_nbr", "class"]], on="item_nbr", how="left")
    d["dept_key"] = d["family"].map(dept_key_by_family)
    d["target_margin_pct"] = d["gross_margin_pct"]
    d["is_perishable"] = d["perishable"].astype(int)
    return d.rename(columns={"unit_price": "retail_price", "case_pack_units": "case_pack_qty"})[
        ["item_nbr", "family", "class", "is_perishable", "shelf_life_days", "unit_cost",
         "retail_price", "target_margin_pct", "case_pack_qty", "cadence_days", "dept_key"]
    ]


def build_dim_vendor(econ):
    rows = []
    for family, cadence in econ.groupby("family")["cadence_days"].first().items():
        mask = "".join("1" if i % int(cadence) == 0 else "0" for i in range(7))
        rows.append(dict(vendor_name=VENDOR_BY_FAMILY[family], lead_time_days=int(cadence), delivery_days_mask=mask))
    return pd.DataFrame(rows)


def build_dim_scenario():
    return pd.DataFrame(SCENARIOS, columns=["scenario_name", "description"])


def build_dim_date(holidays):
    dates = pd.date_range(DATE_START, DATE_END, freq="D")
    df = pd.DataFrame({"date_key": dates.strftime("%Y%m%d").astype(int), "date": dates})
    df["day_of_week"] = dates.strftime("%A")
    df["week_of_year"] = dates.isocalendar().week.to_numpy()
    df["month"] = dates.month
    df["is_weekend"] = (dates.dayofweek >= 5).astype(int)

    h = holidays.copy()
    h["date"] = pd.to_datetime(h["date"])
    national = h[(h["locale"] == "National") & (h["type"] != "Work Day") & (~h["transferred"])]
    national = national.drop_duplicates(subset="date")
    df = df.merge(national[["date", "description"]], on="date", how="left")
    df["is_holiday"] = df["description"].notna().astype(int)
    df = df.rename(columns={"description": "holiday_name"})
    return df[["date_key", "date", "day_of_week", "week_of_year", "month", "is_holiday", "holiday_name", "is_weekend"]]


# ---------------------------------------------------------------- facts

def build_fact_sales(observed, grid, keys):
    df = observed.merge(grid[["store_nbr", "item_nbr", "date", "onpromotion"]], on=["store_nbr", "item_nbr", "date"], how="left")
    df = df.merge(keys["item_econ"], on="item_nbr", how="left")
    df["date_key"] = df["date"].dt.strftime("%Y%m%d").astype(int)
    df["store_key"] = df["store_nbr"].map(keys["store"])
    df["item_key"] = df["item_nbr"].map(keys["item"])
    df["scenario_key"] = keys["scenario"]["Historical"]
    df["units_sold"] = df["unit_sales_observed"].round(2)
    df["gross_revenue"] = (df["units_sold"] * df["unit_price"]).round(2)
    df["cogs"] = (df["units_sold"] * df["unit_cost"]).round(2)
    df["on_promo_flag"] = df["onpromotion"].astype(int)
    return df[["date_key", "store_key", "item_key", "scenario_key", "units_sold", "gross_revenue", "cogs", "on_promo_flag"]]


def build_fact_inventory_snap(observed, velocity, keys):
    df = observed.merge(keys["item_econ"], on="item_nbr", how="left")
    df = df.merge(velocity[["item_nbr", "median_units_per_day"]], on="item_nbr", how="left")
    df["date_key"] = df["date"].dt.strftime("%Y%m%d").astype(int)
    df["store_key"] = df["store_nbr"].map(keys["store"])
    df["item_key"] = df["item_nbr"].map(keys["item"])
    df["scenario_key"] = keys["scenario"]["Historical"]
    df["on_hand_units"] = df["on_hand_book"].round(2)
    df["on_hand_value"] = (df["on_hand_units"] * df["unit_cost"]).round(2)
    df["days_of_supply"] = np.where(
        df["median_units_per_day"] > 0, (df["on_hand_units"] / df["median_units_per_day"]).round(2), np.nan
    )
    return df[["date_key", "store_key", "item_key", "scenario_key", "on_hand_units", "on_hand_value", "days_of_supply"]]


def build_fact_receipts(source, keys):
    common = source.merge(keys["item_econ"], on="item_nbr", how="left")
    common["date_key"] = common["date"].dt.strftime("%Y%m%d").astype(int)
    common["store_key"] = common["store_nbr"].map(keys["store"])
    common["item_key"] = common["item_nbr"].map(keys["item"])
    common["vendor_key"] = common["family"].map(lambda f: keys["vendor"][VENDOR_BY_FAMILY[f]])
    common["scenario_key"] = keys["scenario"]["Historical"]
    common["expiry_date"] = (common["date"] + pd.to_timedelta(common["shelf_life_days"], unit="D")).dt.strftime("%Y-%m-%d")

    regular = common.loc[common["receipts"] > 0].copy()
    regular["ordered_units"] = regular["received_units"] = regular["receipts"].round(2)
    regular["is_emergency_topup"] = 0

    emergency = common.loc[common["emergency_topup"] > 0].copy()
    emergency["ordered_units"] = emergency["received_units"] = emergency["emergency_topup"].round(2)
    emergency["is_emergency_topup"] = 1

    out = pd.concat([regular, emergency], ignore_index=True)
    return out[["date_key", "store_key", "item_key", "vendor_key", "scenario_key",
                "ordered_units", "received_units", "unit_cost", "expiry_date", "is_emergency_topup"]]


def build_fact_waste(source, keys):
    df = source.loc[source["waste"] > 0].merge(keys["item_econ"], on="item_nbr", how="left")
    df["date_key"] = df["date"].dt.strftime("%Y%m%d").astype(int)
    df["store_key"] = df["store_nbr"].map(keys["store"])
    df["item_key"] = df["item_nbr"].map(keys["item"])
    df["scenario_key"] = keys["scenario"]["Historical"]
    df["units_wasted"] = df["waste"].round(2)
    df["waste_cost"] = (df["units_wasted"] * df["unit_cost"]).round(2)
    df["reason"] = "Expired"
    return df[["date_key", "store_key", "item_key", "scenario_key", "units_wasted", "waste_cost", "reason"]]


def build_phantom_events(events, keys):
    df = events.copy()
    df["store_key"] = df["store_nbr"].map(keys["store"])
    df["item_key"] = df["item_nbr"].map(keys["item"])
    df["scenario_key"] = keys["scenario"]["Historical"]
    df["start_date"] = df["start_date"].dt.strftime("%Y-%m-%d")
    df["end_date"] = df["end_date"].dt.strftime("%Y-%m-%d")
    return df[["store_key", "item_key", "scenario_key", "start_date", "end_date"]]


# ---------------------------------------------------------------- driver

def load_dim(cur, name, view, df, key_col):
    write_csv(df, f"{name}.csv")
    return df, key_col


def main():
    t0 = time.time()

    items = pd.read_parquet(STAGING / "items.parquet")
    stores = pd.read_parquet(STAGING / "stores.parquet")
    holidays = pd.read_parquet(STAGING / "holidays_events.parquet")
    econ = pd.read_parquet(STAGING / "item_economics.parquet")
    velocity = pd.read_parquet(STAGING / "item_velocity.parquet")
    grid = pd.read_parquet(STAGING / "sales_grid.parquet")
    backfill = pd.read_parquet(STAGING / "backfill.parquet")
    observed = pd.read_parquet(STAGING / "sales_observed.parquet")
    phantom_events = pd.read_parquet(STAGING / "phantom_events.parquet")

    subset_store_nbrs = backfill["store_nbr"].unique()

    if CSV_DIR.exists():
        for f in CSV_DIR.glob("*.csv"):
            f.unlink()

    run_schema()
    conn = get_conn()
    cur = conn.cursor()
    cur.fast_executemany = False

    # --- dimensions: build -> CSV -> bulk insert -> read back generated keys
    dept_df = build_dim_department(econ)
    write_csv(dept_df, "dim_department.csv")

    store_df = build_dim_store(stores, subset_store_nbrs)
    write_csv(store_df, "dim_store.csv")

    vendor_df = build_dim_vendor(econ)
    write_csv(vendor_df, "dim_vendor.csv")

    scenario_df = build_dim_scenario()
    write_csv(scenario_df, "dim_scenario.csv")

    date_df = build_dim_date(holidays)
    write_csv(date_df, "dim_date.csv")

    sync_csv_dir_to_container()

    bulk_insert(cur, "v_load_dim_department", "dim_department.csv")
    bulk_insert(cur, "v_load_dim_store", "dim_store.csv")
    bulk_insert(cur, "v_load_dim_vendor", "dim_vendor.csv")
    bulk_insert(cur, "v_load_dim_scenario", "dim_scenario.csv")
    bulk_insert(cur, "dim_date", "dim_date.csv")
    conn.commit()

    dept_key_by_family = dict(cur.execute("SELECT dept_name, dept_key FROM dbo.dim_department").fetchall())
    store_key_by_nbr = dict(cur.execute("SELECT store_nbr, store_key FROM dbo.dim_store").fetchall())
    vendor_key_by_name = dict(cur.execute("SELECT vendor_name, vendor_key FROM dbo.dim_vendor").fetchall())
    scenario_key_by_name = dict(cur.execute("SELECT scenario_name, scenario_key FROM dbo.dim_scenario").fetchall())

    # dim_item depends on dept_key, load after the department key readback
    item_df = build_dim_item(items, econ, dept_key_by_family)
    write_csv(item_df, "dim_item.csv")
    sync_csv_dir_to_container()
    bulk_insert(cur, "v_load_dim_item", "dim_item.csv")
    conn.commit()
    item_key_by_nbr = dict(cur.execute("SELECT item_nbr, item_key FROM dbo.dim_item").fetchall())

    keys = dict(
        store=store_key_by_nbr, item=item_key_by_nbr, vendor=vendor_key_by_name,
        scenario=scenario_key_by_name,
        # excludes "family" -- backfill/observed already carry it, and merging
        # it in again just produces family_x/family_y suffix collisions
        item_econ=econ[["item_nbr", "unit_cost", "unit_price", "shelf_life_days"]],
    )

    # --- facts
    # receipts/waste now come from `observed`, not `backfill`: since
    # 07_phantom_injection.py properly re-simulates the FIFO physics with
    # phantom-suppressed demand, `observed` is the corrected reality
    # (stair-step receipts, waste spikes during phantom windows) and
    # `backfill` is only the unperturbed counterfactual baseline.
    fact_sales = build_fact_sales(observed, grid, keys)
    fact_inv = build_fact_inventory_snap(observed, velocity, keys)
    fact_receipts = build_fact_receipts(observed, keys)
    fact_waste = build_fact_waste(observed, keys)
    fact_phantom = build_phantom_events(phantom_events, keys)

    write_csv(fact_sales, "fact_sales.csv")
    write_csv(fact_inv, "fact_inventory_snap.csv")
    write_csv(fact_receipts, "fact_receipts.csv")
    write_csv(fact_waste, "fact_waste.csv")
    write_csv(fact_phantom, "phantom_events.csv")

    sync_csv_dir_to_container()

    bulk_insert(cur, "v_load_fact_sales", "fact_sales.csv")
    bulk_insert(cur, "v_load_fact_inventory_snap", "fact_inventory_snap.csv")
    bulk_insert(cur, "v_load_fact_receipts", "fact_receipts.csv")
    bulk_insert(cur, "v_load_fact_waste", "fact_waste.csv")
    bulk_insert(cur, "v_load_phantom_events", "phantom_events.csv")
    conn.commit()

    # fact_lost_sales, par_levels, detection_log: no source data yet
    # (forward-sim / par-engine / detector haven't run) -- tables exist,
    # rows stay at 0 by design, nothing to bulk insert.

    elapsed = time.time() - t0

    tables = [
        "dim_store", "dim_item", "dim_department", "dim_date", "dim_vendor", "dim_scenario",
        "fact_sales", "fact_inventory_snap", "fact_receipts", "fact_waste", "fact_lost_sales",
        "par_levels", "phantom_events", "detection_log",
    ]
    print("=== 08_load.py row counts ===")
    for t in tables:
        n = cur.execute(f"SELECT COUNT(*) FROM dbo.{t}").fetchone()[0]
        print(f"  {t}: {n:,}")
    print(f"total load time: {elapsed:.1f}s")

    conn.close()


if __name__ == "__main__":
    main()
