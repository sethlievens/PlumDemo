"""
Densify the staged Favorita subset to a full daily grid per (store, item),
bounded by that store-item's own first sale -> last sale. This is the #1 bug
source per CLAUDE.md: train.csv has NO zero-sales rows, so a missing
(store,item,date) means "no sale that day," not "no such row to fill." Every
later inventory calc depends on this grid being complete and correctly
bounded -- filling before first sale or after last sale would invent fake
pre-launch / post-discontinuation history.
"""

import pandas as pd

STAGING = "data/staging"
DATE_START = "2015-01-01"
DATE_END = "2017-08-15"
EARTHQUAKE_START = "2016-04-10"
EARTHQUAKE_END = "2016-05-15"


def valid_calendar():
    all_dates = pd.date_range(DATE_START, DATE_END, freq="D")
    quake = (all_dates >= EARTHQUAKE_START) & (all_dates <= EARTHQUAKE_END)
    return all_dates[~quake]


def densify(subset, valid_dates):
    subset = subset.copy()
    subset["date"] = pd.to_datetime(subset["date"])
    subset["onpromotion"] = subset["onpromotion"].isin([True, "True", "true", 1, "1"])

    lifecycles = (
        subset.groupby(["store_nbr", "item_nbr"])["date"]
        .agg(first_sale="min", last_sale="max")
        .reset_index()
    )

    grid_parts = [
        pd.DataFrame(
            {
                "store_nbr": r.store_nbr,
                "item_nbr": r.item_nbr,
                "date": valid_dates[(valid_dates >= r.first_sale) & (valid_dates <= r.last_sale)],
            }
        )
        for r in lifecycles.itertuples()
    ]
    grid = pd.concat(grid_parts, ignore_index=True)

    grid = grid.merge(
        subset[["store_nbr", "item_nbr", "date", "unit_sales", "returns", "onpromotion"]],
        on=["store_nbr", "item_nbr", "date"],
        how="left",
    )
    grid["observed"] = grid["unit_sales"].notna()
    grid["unit_sales"] = grid["unit_sales"].fillna(0.0).astype("float32")
    grid["returns"] = grid["returns"].fillna(0.0).astype("float32")
    grid["onpromotion"] = grid["onpromotion"].fillna(False)

    return grid, lifecycles


def validate(subset, grid, lifecycles, valid_dates):
    checks = []

    dupes = grid.duplicated(["store_nbr", "item_nbr", "date"]).sum()
    checks.append(("no duplicate (store,item,date) rows", dupes == 0))

    bounds = grid.merge(lifecycles, on=["store_nbr", "item_nbr"], how="left")
    in_bounds = ((bounds["date"] >= bounds["first_sale"]) & (bounds["date"] <= bounds["last_sale"])).all()
    checks.append(("all grid dates within own store-item lifecycle", bool(in_bounds)))

    checks.append(("no grid dates in earthquake window", not grid["date"].between(EARTHQUAKE_START, EARTHQUAKE_END).any()))

    checks.append(("original subset row count preserved as observed rows", grid["observed"].sum() == len(subset)))

    checks.append(("unit_sales non-negative", (grid["unit_sales"] >= 0).all()))
    checks.append(("returns non-negative", (grid["returns"] >= 0).all()))

    checks.append(("grid row count == sum of per-store-item lifecycle lengths", len(grid) == sum(len(p) for p in [
        valid_dates[(valid_dates >= r.first_sale) & (valid_dates <= r.last_sale)] for r in lifecycles.itertuples()
    ])))

    return checks


def main():
    subset = pd.read_parquet(f"{STAGING}/train_subset.parquet")
    valid_dates = valid_calendar()

    grid, lifecycles = densify(subset, valid_dates)
    grid.to_parquet(f"{STAGING}/sales_grid.parquet", index=False)

    for name, ok in validate(subset, grid, lifecycles, valid_dates):
        print(f"{'PASS' if ok else 'FAIL'}: {name}")


if __name__ == "__main__":
    main()
