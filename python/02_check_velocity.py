"""
Sanity check on the 01_subset.py staged data: is there enough per-item daily
velocity in each family to support a production-planning story (DELI, BAKERY
in particular need real fast-movers, not a pile of slow tail items)?

Velocity is measured per (store, item), not pooled across the 6 staged
stores -- production planning happens at the store level (one bakery case,
one deli counter), so pooling first would let an item carried thinly at 6
stores look like a fast-mover when no single store actually moves it.

For each (store, item), densifies to that store-item's own lifecycle window
(first sale -> last sale, earthquake gap excluded -- same grid rule as
CLAUDE.md #1 bug source) and fills non-selling days with 0. All densified
store-days for an item are pooled and the median taken -- that's the
per-item velocity. Grouped by family.
"""

from collections import defaultdict

import pandas as pd

STAGING = "data/staging"
DATE_START = "2015-01-01"
DATE_END = "2017-08-15"
EARTHQUAKE_START = "2016-04-10"
EARTHQUAKE_END = "2016-05-15"
FAST_MOVER_THRESHOLD = 2.0  # units/day
FAST_MOVER_GATE = 15  # min fast-movers required for DELI/BAKERY to support production planning


def build_valid_dates():
    all_dates = pd.date_range(DATE_START, DATE_END, freq="D")
    quake = (all_dates >= EARTHQUAKE_START) & (all_dates <= EARTHQUAKE_END)
    return all_dates[~quake]


def item_velocity(subset, valid_dates):
    subset = subset.copy()
    subset["date"] = pd.to_datetime(subset["date"])
    daily = subset.groupby(["item_nbr", "store_nbr", "date"])["unit_sales"].sum()

    pooled = defaultdict(list)
    for (item_nbr, store_nbr), series in daily.groupby(level=[0, 1]):
        series = series.droplevel([0, 1])
        lifecycle = valid_dates[(valid_dates >= series.index.min()) & (valid_dates <= series.index.max())]
        densified = series.reindex(lifecycle, fill_value=0.0)
        pooled[item_nbr].append(densified)

    medians = {item_nbr: pd.concat(parts).median() for item_nbr, parts in pooled.items()}
    return pd.Series(medians, name="median_units_per_day")


def main():
    subset = pd.read_parquet(f"{STAGING}/train_subset.parquet")
    items = pd.read_parquet(f"{STAGING}/items.parquet")
    valid_dates = build_valid_dates()

    vel = item_velocity(subset, valid_dates).reset_index().rename(columns={"index": "item_nbr"})
    vel = vel.merge(items[["item_nbr", "family"]], on="item_nbr", how="left")

    print("=== 02_check_velocity.py: per-item daily velocity by family ===")
    print(f"{'family':<12} {'n_items':>7} {'median':>8} {'>=2/day':>8} {'pct':>6}")
    gate_results = {}
    for fam, g in vel.groupby("family"):
        n = len(g)
        fast = (g["median_units_per_day"] >= FAST_MOVER_THRESHOLD).sum()
        pct = 100 * fast / n
        print(f"{fam:<12} {n:>7} {g['median_units_per_day'].median():>8.2f} {fast:>8} {pct:>5.0f}%")
        gate_results[fam] = fast

    print()
    print("=== validation gate ===")
    all_pass = True
    for fam in ["DELI", "BREAD/BAKERY"]:
        fast = gate_results.get(fam, 0)
        ok = fast >= FAST_MOVER_GATE
        all_pass &= ok
        status = "PASS" if ok else "FAIL"
        print(f"{fam} fast-movers >= {FAST_MOVER_GATE} (>= {FAST_MOVER_THRESHOLD}/day): {fast} -- {status}")
    print("OVERALL:", "PASS" if all_pass else "FAIL")

    vel.to_parquet(f"{STAGING}/item_velocity.parquet", index=False)
    return all_pass


if __name__ == "__main__":
    main()
