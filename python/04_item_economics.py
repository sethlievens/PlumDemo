"""
Favorita has no cost, price, shelf-life, case-pack, or delivery-cadence data
-- only sales. This synthesizes a per-item economics table from family-level
grocery-industry assumptions (GROCERY I/II already folded to one GROCERY
family upstream in 01_subset.py), so every downstream step has what it needs
to turn a sales curve into an inventory curve.

Per-item price/cost carry lognormal jitter around the family baseline so the
400 items aren't all identical within a family. Blended gross margin is
revenue-weighted using realized sales volume from the densified grid and
must land in the 28-35% band CLAUDE.md anchors to industry norms.
"""

import numpy as np
import pandas as pd

STAGING = "data/staging"
SEED = 42

# shelf_life_days: realistic shelf window before waste.
# cadence_days: days between deliveries (<= shelf_life so a delivery cycle
#   can actually be covered without automatic spoilage).
# case_pack: units per delivered case, receipts round up to this.
# price / margin: family baseline; jitter applied per item below.
FAMILY_ECON = {
    "PRODUCE":      dict(shelf_life_days=4,   cadence_days=2,  case_pack=10, price=1.20, margin=0.26),
    "DAIRY":        dict(shelf_life_days=14,  cadence_days=3,  case_pack=12, price=3.00, margin=0.28),
    "DELI":         dict(shelf_life_days=2,   cadence_days=1,  case_pack=1,  price=5.50, margin=0.33),
    "BREAD/BAKERY": dict(shelf_life_days=2,   cadence_days=1,  case_pack=1,  price=3.50, margin=0.40),
    "BEVERAGES":    dict(shelf_life_days=270, cadence_days=7,  case_pack=24, price=2.00, margin=0.26),
    "GROCERY":      dict(shelf_life_days=365, cadence_days=10, case_pack=24, price=3.00, margin=0.35),
}
PRICE_SIGMA = 0.20
COST_SIGMA = 0.05
MARGIN_BAND = (0.28, 0.35)


def build_economics(item_nbrs, items, rng):
    econ = items.loc[items["item_nbr"].isin(item_nbrs), ["item_nbr", "family", "perishable"]].copy()
    econ = econ[econ["family"].isin(FAMILY_ECON)].reset_index(drop=True)

    fam_df = pd.DataFrame(FAMILY_ECON).T.reset_index().rename(columns={"index": "family"})
    econ = econ.merge(fam_df, on="family", how="left")

    n = len(econ)
    price_jitter = rng.lognormal(mean=0.0, sigma=PRICE_SIGMA, size=n)
    cost_jitter = rng.lognormal(mean=0.0, sigma=COST_SIGMA, size=n)

    econ["unit_price"] = (econ["price"] * price_jitter).round(2).clip(lower=0.10)
    econ["unit_cost"] = (econ["unit_price"] * (1 - econ["margin"]) * cost_jitter).round(2).clip(lower=0.05)
    econ["unit_cost"] = np.minimum(econ["unit_cost"], econ["unit_price"] * 0.95)
    econ["gross_margin_pct"] = (econ["unit_price"] - econ["unit_cost"]) / econ["unit_price"]

    econ["shelf_life_days"] = econ["shelf_life_days"].astype(int)
    econ["cadence_days"] = econ["cadence_days"].astype(int)
    econ["case_pack_units"] = econ["case_pack"].astype(int)

    return econ[[
        "item_nbr", "family", "perishable", "shelf_life_days", "cadence_days",
        "case_pack_units", "unit_cost", "unit_price", "gross_margin_pct",
    ]]


def blended_margin(econ, grid):
    volume = grid.groupby("item_nbr")["unit_sales"].sum().rename("total_units")
    priced = econ.merge(volume, on="item_nbr", how="left").fillna({"total_units": 0.0})
    revenue = priced["total_units"] * priced["unit_price"]
    cost = priced["total_units"] * priced["unit_cost"]
    return 1 - cost.sum() / revenue.sum()


def main():
    items = pd.read_parquet(f"{STAGING}/items.parquet")
    grid = pd.read_parquet(f"{STAGING}/sales_grid.parquet")
    rng = np.random.default_rng(SEED)

    item_nbrs = grid["item_nbr"].unique()
    econ = build_economics(item_nbrs, items, rng)
    econ.to_parquet(f"{STAGING}/item_economics.parquet", index=False)

    checks = []
    checks.append(("every grid item has an economics row", set(item_nbrs) == set(econ["item_nbr"])))
    checks.append(("no GROCERY I / GROCERY II split remains", not econ["family"].isin(["GROCERY I", "GROCERY II"]).any()))
    checks.append(("unit_cost < unit_price for all items", (econ["unit_cost"] < econ["unit_price"]).all()))
    checks.append(("shelf_life_days >= cadence_days for all items", (econ["shelf_life_days"] >= econ["cadence_days"]).all()))
    checks.append(("case_pack_units >= 1 for all items", (econ["case_pack_units"] >= 1).all()))

    blend = blended_margin(econ, grid)
    lo, hi = MARGIN_BAND
    checks.append((f"blended gross margin {blend:.1%} in [{lo:.0%},{hi:.0%}]", lo <= blend <= hi))

    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}: {name}")

    assert all(ok for _, ok in checks), "validation gate failed -- do not proceed to 05_backfill.py"


if __name__ == "__main__":
    main()
