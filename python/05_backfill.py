"""
Inverted backfill per docs/BACKFILL.md: define the demand curve, derive
receipts as the residual needed to hit a target on-hand, then roll forward
with FIFO lots. Never simulate forward from a guessed receipt stream -- one
demand spike sends that approach negative and there's nothing to tune back.
Inverting it means the accounting identity holds by construction.

Forecast uses ONLY trailing 28-day velocity, never forward sales, plus a
per-family lognormal error term (mean-1, doc range sigma 0.15-0.28). A buyer
with perfect foresight would carry zero waste, which no real store does --
the error term IS the shrink model.

Simulated across all ~2,175 store-item series at once, one Python-level step
per calendar day (~880 total). The recurrence (today's on-hand depends on
yesterday's) can't be avoided, but nothing loops row-by-row over the
sales_grid fact table -- each day-step is a handful of numpy ops over the
full (day x store-item) matrix.

Exposes run_backfill() so 06_calibrate.py can re-run this with a different
per-family buffer without duplicating the simulation.
"""

import numpy as np
import pandas as pd

STAGING = "data/staging"
SEED = 42
TRAILING_WINDOW = 28

# lognormal forecast-error sigma per family, doc range 0.15-0.28.
# Produce is the least predictable (weather/quality driven); dry grocery and
# beverages are the most stable.
FAMILY_SIGMA = {
    "PRODUCE": 0.28,
    "DAIRY": 0.18,
    "DELI": 0.22,
    "BREAD/BAKERY": 0.25,
    "BEVERAGES": 0.15,
    "GROCERY": 0.15,
}

# Starting guess for the per-family buffer multiplier; 06_calibrate.py's
# proportional controller starts here and tunes against the CLAUDE.md
# waste%/days-of-supply anchors. Uniform 1.65 is just the value that clears
# this script's own emergency-top-up gate at the starting point.
DEFAULT_BUFFER = {fam: 1.65 for fam in FAMILY_SIGMA}

# Calibrated buffers, accepted after 06_calibrate.py's frontier-sweep-verified
# proportional-control pass. DAIRY and GROCERY fully converge (waste,
# days-of-supply, and top-up all clean). PRODUCE and BREAD/BAKERY converge on
# waste but sit a few points over the top-up gate at their own family level
# (a controller near-miss, not a wall). DELI converges on waste but hits a
# ~7.5% top-up floor confirmed structural -- no buffer clears it, see
# docs/FINDINGS.md. Frozen as-is rather than chased further.
CALIBRATED_BUFFER = {
    "PRODUCE": 1.65,
    "DAIRY": 2.58,
    "DELI": 1.65,
    "BREAD/BAKERY": 1.65,
    "BEVERAGES": 1.65,
    "GROCERY": 2.06,
}


def load_inputs():
    grid = pd.read_parquet(f"{STAGING}/sales_grid.parquet")
    econ = pd.read_parquet(f"{STAGING}/item_economics.parquet")
    return grid, econ


def build_matrices(grid, econ):
    valid_dates = pd.DatetimeIndex(sorted(grid["date"].unique()))
    T = len(valid_dates)
    date_idx = {d: i for i, d in enumerate(valid_dates)}

    store_items = grid[["store_nbr", "item_nbr"]].drop_duplicates().reset_index(drop=True)
    store_items = store_items.merge(econ, on="item_nbr", how="left")
    N = len(store_items)
    cols = list(zip(store_items["store_nbr"], store_items["item_nbr"]))

    sales_wide = (
        grid.pivot(index="date", columns=["store_nbr", "item_nbr"], values="unit_sales")
        .reindex(valid_dates)
        .fillna(0.0)
    )
    S = sales_wide[cols].to_numpy(dtype=np.float32)  # (T, N)

    lifecycles = grid.groupby(["store_nbr", "item_nbr"])["date"].agg(first_sale="min", last_sale="max").loc[cols]
    first_idx = np.array([date_idx[d] for d in lifecycles["first_sale"]])
    last_idx = np.array([date_idx[d] for d in lifecycles["last_sale"]])

    t_arr = np.arange(T)[:, None]
    active = (t_arr >= first_idx[None, :]) & (t_arr <= last_idx[None, :])
    dow = valid_dates.dayofweek.to_numpy()

    return dict(
        valid_dates=valid_dates, T=T, N=N, S=S, active=active, dow=dow,
        first_idx=first_idx, store_items=store_items,
    )


def family_arrays(store_items, buffer_by_family):
    fam_list = sorted(FAMILY_SIGMA)
    families = store_items["family"].to_numpy()
    fam_idx = np.array([fam_list.index(f) for f in families])

    shelf_life = store_items["shelf_life_days"].to_numpy(dtype=np.int32)
    cadence = store_items["cadence_days"].to_numpy(dtype=np.float32)
    case_pack = store_items["case_pack_units"].to_numpy(dtype=np.float32)
    sigma = np.array([FAMILY_SIGMA[f] for f in families], dtype=np.float32)
    mu = -0.5 * sigma**2  # keeps E[lognormal error] == 1, i.e. unbiased on average
    buffer = np.array([buffer_by_family[f] for f in families], dtype=np.float32)

    return dict(fam_list=fam_list, fam_idx=fam_idx, shelf_life=shelf_life, cadence=cadence,
                case_pack=case_pack, sigma=sigma, mu=mu, buffer=buffer)


def dow_index_table(grid, econ, fam_list):
    g = grid.merge(econ[["item_nbr", "family"]], on="item_nbr", how="left")
    g["dow"] = pd.to_datetime(g["date"]).dt.dayofweek
    by_fam_dow = g.groupby(["family", "dow"])["unit_sales"].mean().unstack("dow")
    by_fam = g.groupby("family")["unit_sales"].mean()
    idx = by_fam_dow.div(by_fam, axis=0).reindex(fam_list)
    return idx.to_numpy(dtype=np.float32)


def trailing_velocity(S, valid_dates):
    sales_df = pd.DataFrame(S, index=valid_dates)
    vel = sales_df.shift(1).rolling(TRAILING_WINDOW, min_periods=1).mean().fillna(0.0)
    return vel.to_numpy(dtype=np.float32)


def delivery_mask(T, N, first_idx, cadence, active):
    t_arr = np.arange(T)[:, None]
    is_delivery = ((t_arr - first_idx[None, :]) % cadence[None, :].astype(np.int64)) == 0
    return is_delivery & active


def simulate(mats, fam, dow_table, seed):
    T, N, S, active, dow = mats["T"], mats["N"], mats["S"], mats["active"], mats["dow"]
    delivery = delivery_mask(T, N, mats["first_idx"], fam["cadence"], active)
    velocity = trailing_velocity(S, mats["valid_dates"])

    shelf_life, cadence, case_pack = fam["shelf_life"], fam["cadence"], fam["case_pack"]
    buffer, mu, sigma, fam_idx = fam["buffer"], fam["mu"], fam["sigma"], fam["fam_idx"]

    SMAX = int(shelf_life.max()) + 1
    buckets = np.zeros((SMAX, N), dtype=np.float32)  # buckets[age, store_item]; age 0 = freshest
    col_idx = np.arange(N)
    rng = np.random.default_rng(seed)

    receipts = np.zeros((T, N), dtype=np.float32)
    waste = np.zeros((T, N), dtype=np.float32)
    emergency = np.zeros((T, N), dtype=np.float32)
    closing = np.zeros((T, N), dtype=np.float32)

    for t in range(T):
        # 1. age everything by a day; the fresh slot is refilled below if there's a delivery
        buckets = np.roll(buckets, 1, axis=0)
        buckets[0, :] = 0.0

        # 2. expire whatever just reached its own shelf-life age -> waste
        waste_today = buckets[shelf_life, col_idx].copy()
        buckets[shelf_life, col_idx] = 0.0

        # 3. receipts = residual needed to hit target on-hand, delivery days only, case-pack rounded
        opening = buckets.sum(axis=0)
        dow_idx_t = dow_table[fam_idx, dow[t]]
        error = rng.lognormal(mean=mu, sigma=sigma, size=N)
        forecast = velocity[t] * dow_idx_t * cadence * error
        target = np.minimum(forecast * buffer, velocity[t] * shelf_life)
        receipt_needed = np.maximum(0.0, target - opening)
        receipt_qty = np.where(delivery[t], np.ceil(receipt_needed / case_pack) * case_pack, 0.0)
        buckets[0, :] += receipt_qty

        # 4. consume today's ground-truth sales, oldest lot first; shortfall -> logged emergency top-up
        available = buckets.sum(axis=0)
        required = S[t]
        actual_consumed = np.minimum(available, required)
        emergency_today = np.maximum(0.0, required - available)

        reversed_buckets = buckets[::-1, :]
        cum = np.cumsum(reversed_buckets, axis=0)
        prior = np.vstack([np.zeros((1, N), dtype=np.float32), cum[:-1, :]])
        removed = np.clip(actual_consumed[None, :] - prior, 0.0, reversed_buckets)
        buckets = (reversed_buckets - removed)[::-1, :]

        receipts[t], waste[t], emergency[t], closing[t] = receipt_qty, waste_today, emergency_today, buckets.sum(axis=0)

    return receipts, waste, emergency, closing


def to_long(mats, closing, receipts, waste, emergency):
    store_items, dates, active = mats["store_items"], mats["valid_dates"], mats["active"]
    idx_t, idx_n = np.where(active)
    return pd.DataFrame({
        "store_nbr": store_items["store_nbr"].to_numpy()[idx_n],
        "item_nbr": store_items["item_nbr"].to_numpy()[idx_n],
        "family": store_items["family"].to_numpy()[idx_n],
        "date": dates.to_numpy()[idx_t],
        "unit_sales": mats["S"][idx_t, idx_n],
        "receipts": receipts[idx_t, idx_n],
        "waste": waste[idx_t, idx_n],
        "emergency_topup": emergency[idx_t, idx_n],
        "on_hand": closing[idx_t, idx_n],
    })


def compute_metrics(mats, closing, receipts, waste, emergency):
    S, active, N = mats["S"], mats["active"], mats["N"]
    active_days = active.sum(axis=0)

    total_sales = S.sum(axis=0)
    total_receipts = receipts.sum(axis=0)
    total_waste = waste.sum(axis=0)
    total_emergency_days = (emergency > 0).sum(axis=0)
    avg_on_hand = np.divide((closing * active).sum(axis=0), active_days, out=np.zeros(N), where=active_days > 0)
    avg_velocity = np.divide(total_sales, active_days, out=np.zeros(N), where=active_days > 0)
    days_of_supply = np.divide(avg_on_hand, avg_velocity, out=np.zeros(N), where=avg_velocity > 0)
    emergency_rate = np.divide(total_emergency_days, active_days, out=np.zeros(N), where=active_days > 0)

    summary = mats["store_items"][["store_nbr", "item_nbr", "family"]].copy()
    summary["total_sales"] = total_sales
    summary["total_receipts"] = total_receipts
    summary["total_waste"] = total_waste
    summary["avg_on_hand"] = avg_on_hand
    summary["avg_velocity"] = avg_velocity
    summary["days_of_supply"] = days_of_supply
    summary["emergency_rate"] = emergency_rate

    by_family = summary.groupby("family").agg(
        n_items=("item_nbr", "nunique"),
        total_sales=("total_sales", "sum"),
        total_receipts=("total_receipts", "sum"),
        total_waste=("total_waste", "sum"),
        days_of_supply_unweighted=("days_of_supply", "mean"),
        sum_avg_on_hand=("avg_on_hand", "sum"),
        sum_avg_velocity=("avg_velocity", "sum"),
        emergency_rate=("emergency_rate", "mean"),
    )
    # unweighted mean treats a 0.01-unit/day item the same as a 20-unit/day item;
    # volume-weighted (sum on-hand / sum velocity across items) is what a buyer
    # actually experiences and is what CLAUDE.md's anchors are calibrated against.
    # Keep both -- the spread between them flags case-pack-driven distortion.
    by_family["days_of_supply_weighted"] = by_family["sum_avg_on_hand"] / by_family["sum_avg_velocity"]
    by_family["waste_pct"] = 100 * by_family["total_waste"] / by_family["total_receipts"]
    by_family = by_family.drop(columns=["sum_avg_on_hand", "sum_avg_velocity"])
    return summary, by_family


def identity_check(mats, closing, receipts, waste, emergency):
    N, S, active = mats["N"], mats["S"], mats["active"]
    prior_closing = np.vstack([np.zeros((1, N), dtype=np.float32), closing[:-1]])
    implied = prior_closing + receipts - waste - (S - emergency)
    error = closing - implied

    active_cells = int(active.sum())
    emergency_cells = int((emergency[active] > 0).sum())
    return dict(
        max_abs_error=float(np.abs(error[active]).max()) if active_cells else 0.0,
        on_hand_min=float(closing[active].min()) if active_cells else 0.0,
        emergency_rate=emergency_cells / active_cells if active_cells else 0.0,
    )


def run_backfill(buffer_by_family, seed=SEED):
    grid, econ = load_inputs()
    mats = build_matrices(grid, econ)
    fam = family_arrays(mats["store_items"], buffer_by_family)
    dow_table = dow_index_table(grid, econ, fam["fam_list"])

    receipts, waste, emergency, closing = simulate(mats, fam, dow_table, seed)
    results = to_long(mats, closing, receipts, waste, emergency)
    summary, by_family = compute_metrics(mats, closing, receipts, waste, emergency)
    diagnostics = identity_check(mats, closing, receipts, waste, emergency)
    return results, by_family, diagnostics


def main():
    results, by_family, diag = run_backfill(CALIBRATED_BUFFER)
    results.to_parquet(f"{STAGING}/backfill.parquet", index=False)
    by_family.to_parquet(f"{STAGING}/backfill_family_summary.parquet")

    checks = [
        ("on_hand never negative", diag["on_hand_min"] >= -1e-3),
        ("accounting identity holds (max err < 0.01 units)", diag["max_abs_error"] < 1e-2),
        (f"emergency top-up rate < 5% (actual {diag['emergency_rate']:.1%})", diag["emergency_rate"] < 0.05),
    ]
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
    assert all(ok for _, ok in checks), "validation gate failed on the frozen calibrated buffer"


if __name__ == "__main__":
    main()
