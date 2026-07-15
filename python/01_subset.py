"""
Subset the Favorita train.csv (4.7GB, ~125M rows) down to a demo-sized slice
and stage it plus the dimension files as parquet under data/staging/.

Streams train.csv in chunks and only ever keeps the 6-store / date-window
subset in memory (a few million rows) -- never the full file.

Store selection: 6 stores hand-picked to span 5 of 5 store types and 6
distinct clusters (CLAUDE.md requires >=2 of each; this goes further so the
cross-store phantom detector has a real peer-group spread to work with).

Item selection: top items by volume within the store/date subset, stratified
by family bucket so perishables aren't crowded out by dry grocery (which
dominates the raw item count 1348/4101).
"""

import numpy as np
import pandas as pd

RAW = "data/raw"
STAGING = "data/staging"

DATE_START = "2015-01-01"
DATE_END = "2017-08-15"
EARTHQUAKE_START = "2016-04-10"
EARTHQUAKE_END = "2016-05-15"

SELECTED_STORES = [44, 34, 13, 41, 28, 3]  # type A,B,C,D,E,D / cluster 5,6,15,4,10,8

FAMILY_FOLD = {"GROCERY I": "GROCERY", "GROCERY II": "GROCERY"}

FAMILY_BUCKETS = {
    "PRODUCE": "produce",
    "DAIRY": "dairy",
    "DELI": "deli",
    "BREAD/BAKERY": "bakery",
    "BEVERAGES": "beverages",
    "GROCERY": "dry",
}
ITEM_QUOTA = {
    "produce": 60,
    "dairy": 50,
    "deli": 40,
    "bakery": 40,
    "beverages": 60,
    "dry": 150,
}

CHUNKSIZE = 2_000_000
TRAIN_COLS = ["date", "store_nbr", "item_nbr", "unit_sales", "onpromotion"]
TRAIN_DTYPES = {
    "date": "string",
    "store_nbr": "int16",
    "item_nbr": "int32",
    "unit_sales": "float32",
    "onpromotion": "object",
}


def stream_store_date_subset():
    """Pass over train.csv once, keeping only rows in our stores + date window."""
    chunks = []
    reader = pd.read_csv(
        f"{RAW}/train.csv",
        usecols=TRAIN_COLS,
        dtype=TRAIN_DTYPES,
        chunksize=CHUNKSIZE,
    )
    for chunk in reader:
        in_stores = chunk["store_nbr"].isin(SELECTED_STORES)
        in_window = (chunk["date"] >= DATE_START) & (chunk["date"] <= DATE_END)
        in_earthquake = (chunk["date"] >= EARTHQUAKE_START) & (chunk["date"] <= EARTHQUAKE_END)
        keep = in_stores & in_window & ~in_earthquake
        if keep.any():
            chunks.append(chunk.loc[keep])
    return pd.concat(chunks, ignore_index=True)


def select_items(subset, items):
    volume = (
        subset.loc[subset["unit_sales"] > 0]
        .groupby("item_nbr")["unit_sales"]
        .sum()
        .rename("volume")
        .reset_index()
    )
    volume = volume.merge(items[["item_nbr", "family"]], on="item_nbr", how="left")
    volume["bucket"] = volume["family"].map(FAMILY_BUCKETS)
    volume = volume.dropna(subset=["bucket"])

    selected = []
    for bucket, quota in ITEM_QUOTA.items():
        top = volume.loc[volume["bucket"] == bucket].nlargest(quota, "volume")
        selected.append(top)
    return pd.concat(selected, ignore_index=True)["item_nbr"].tolist()


def main():
    items = pd.read_csv(f"{RAW}/items.csv")
    items["family"] = items["family"].replace(FAMILY_FOLD)
    stores = pd.read_csv(f"{RAW}/stores.csv")
    transactions = pd.read_csv(f"{RAW}/transactions.csv")
    holidays = pd.read_csv(f"{RAW}/holidays_events.csv")

    subset = stream_store_date_subset()
    selected_items = select_items(subset, items)
    subset = subset.loc[subset["item_nbr"].isin(selected_items)].copy()

    subset["returns"] = (-subset["unit_sales"]).clip(lower=0).astype("float32")
    subset["unit_sales"] = subset["unit_sales"].clip(lower=0).astype("float32")

    subset.to_parquet(f"{STAGING}/train_subset.parquet", index=False)
    items.to_parquet(f"{STAGING}/items.parquet", index=False)
    stores.to_parquet(f"{STAGING}/stores.parquet", index=False)
    transactions.to_parquet(f"{STAGING}/transactions.parquet", index=False)
    holidays.to_parquet(f"{STAGING}/holidays_events.parquet", index=False)

    summarize(subset, items, stores, selected_items)


def summarize(subset, items, stores, selected_items):
    store_info = stores.loc[stores["store_nbr"].isin(subset["store_nbr"].unique())]
    fam_counts = (
        items.loc[items["item_nbr"].isin(selected_items)]["family"]
        .value_counts()
        .sort_values(ascending=False)
    )

    n_dates = subset["date"].nunique()
    n_stores = subset["store_nbr"].nunique()
    n_items = subset["item_nbr"].nunique()
    grid_cells = n_stores * n_items * n_dates
    sparsity = 100 * len(subset) / grid_cells

    print("=== 01_subset.py summary ===")
    print(f"rows staged: {len(subset):,}")
    print(f"date range: {subset['date'].min()} .. {subset['date'].max()} ({n_dates} days)")
    print("stores (store_nbr: type/cluster):")
    for _, r in store_info.sort_values("store_nbr").iterrows():
        print(f"  {r['store_nbr']}: {r['type']}/{r['cluster']} ({r['city']})")
    print(f"items: {n_items} across {len(fam_counts)} families")
    for fam, n in fam_counts.items():
        print(f"  {fam}: {n}")
    print(f"grid density: {len(subset):,} / {grid_cells:,} cells present ({sparsity:.1f}%)")


if __name__ == "__main__":
    main()
