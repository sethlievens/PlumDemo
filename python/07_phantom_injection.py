"""
Injects synthetic phantom-inventory events with CORRECT physics: re-runs
05_backfill.py's actual FIFO simulation with demand forced to zero during
each selected event window, rather than masking a column after the fact.

The physical model (this replaced an earlier, physically incoherent version
that just copied on_hand through unchanged -- see docs/FINDINGS.md):
  - Sales stop ringing during the event -> consumption is genuinely zero,
    not just hidden. Depletion is driven by sales, so on_hand STOPS
    DEPLETING from consumption.
  - Receipts keep arriving on the item's normal cadence, because the
    ordering system sees healthy book stock and has no reason to react --
    it doesn't know anything is wrong.
  - So on_hand STAIR-STEPS UPWARD through the window: flat between
    deliveries, jumping at each receipt.
  - FIFO lots keep aging untouched -> waste rises, especially for
    short-shelf-life families where a lot can actually expire within a
    2-6 day window.
  - After the event ends, real sales resume and the accumulated backlog
    drains back down through ordinary consumption (and reduced future
    receipts, since the residual-based receipt formula naturally throttles
    once on_hand is elevated).

Implementation: reuses 05_backfill.py's build_matrices/family_arrays/
dow_index_table/simulate/to_long unchanged. The ONLY input that differs
between the baseline run and the phantom run is the demand matrix S --
phantom-window cells are zeroed before simulate() ever sees them, so every
downstream consequence (receipts, aging, waste, drain-down) falls out of
the same FIFO mechanics already validated in 05_backfill.py, not
hand-coded here.

Event SELECTION (which store-item-weeks get an event) still uses the
baseline, unperturbed simulation -- an unbiased reference for "was there
real stock to begin with," per the same on_hand > 0 requirement as before.
"""

import importlib.util

import numpy as np
import pandas as pd

spec = importlib.util.spec_from_file_location("backfill", "python/05_backfill.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)

STAGING = "data/staging"
SELECTION_SEED = 7  # which store-item-weeks get an event; separate from bf.SEED (simulation's own lognormal draws)
PHANTOM_RATE = 0.0075
MIN_DURATION, MAX_DURATION = 2, 6


def build_week_universe(baseline_long):
    df = baseline_long[["store_nbr", "item_nbr", "date"]].copy()
    df["week_start"] = df["date"].dt.to_period("W-MON").dt.start_time
    return df[["store_nbr", "item_nbr", "week_start"]].drop_duplicates().reset_index(drop=True)


def sample_phantom_weeks(weeks, rng):
    n = len(weeks)
    k = round(n * PHANTOM_RATE)
    idx = rng.choice(n, size=k, replace=False)
    return weeks.iloc[idx].reset_index(drop=True)


def choose_events(selected_weeks, baseline_long, valid_dates, rng):
    """Selection uses the BASELINE (unperturbed) simulation -- picking where
    to inject an event should be independent of the event's own effects.

    Duration is counted in TRADING days (valid_dates), not raw calendar
    days: a naive start_date + N-days calendar calc can land end_date
    inside the excluded 2016-04-10..2016-05-15 earthquake gap even when
    start_date itself is a valid trading day, since the gap sits in the
    middle of most store-items' lifecycles, not at the edges."""
    lifecycles = baseline_long.groupby(["store_nbr", "item_nbr"])["date"].agg(first_sale="min", last_sale="max")
    on_hand_lookup = baseline_long.set_index(["store_nbr", "item_nbr", "date"])["on_hand"]
    valid_dates_arr = valid_dates.to_numpy()

    events = []
    for row in selected_weeks.itertuples(index=False):
        week_dates = pd.date_range(row.week_start, periods=7, freq="D")
        first_sale, last_sale = lifecycles.loc[(row.store_nbr, row.item_nbr)]
        week_dates = week_dates[(week_dates >= first_sale) & (week_dates <= last_sale)]
        if len(week_dates) == 0:
            continue

        candidates = [
            d for d in week_dates
            if on_hand_lookup.get((row.store_nbr, row.item_nbr, d), 0.0) > 0
            and (last_sale - d).days + 1 >= MIN_DURATION
        ]
        if not candidates:
            continue

        start_date = candidates[rng.integers(0, len(candidates))]
        duration = int(rng.integers(MIN_DURATION, MAX_DURATION + 1))

        start_pos = np.searchsorted(valid_dates_arr, np.datetime64(start_date))
        last_sale_pos = np.searchsorted(valid_dates_arr, np.datetime64(last_sale))
        end_pos = min(start_pos + duration - 1, last_sale_pos)
        end_date = pd.Timestamp(valid_dates_arr[end_pos])

        events.append(dict(
            store_nbr=row.store_nbr, item_nbr=row.item_nbr, week_start=row.week_start,
            start_date=start_date, end_date=end_date, duration_days=end_pos - start_pos + 1,
        ))
    return pd.DataFrame(events)


def build_phantom_mask(mats, events):
    """(T, N) boolean matrix marking every (date, store-item) cell inside a
    selected event's window -- this is the only thing that changes between
    the baseline and phantom-perturbed simulation runs."""
    date_idx = {d: i for i, d in enumerate(mats["valid_dates"])}
    col_idx = {(r.store_nbr, r.item_nbr): i for i, r in enumerate(mats["store_items"].itertuples())}
    mask = np.zeros((mats["T"], mats["N"]), dtype=bool)
    for ev in events.itertuples(index=False):
        col = col_idx[(ev.store_nbr, ev.item_nbr)]
        mask[date_idx[ev.start_date]:date_idx[ev.end_date] + 1, col] = True
    return mask


def mask_to_frame(mats, mask):
    idx_t, idx_n = np.where(mask)
    return pd.DataFrame({
        "store_nbr": mats["store_items"]["store_nbr"].to_numpy()[idx_n],
        "item_nbr": mats["store_items"]["item_nbr"].to_numpy()[idx_n],
        "date": mats["valid_dates"].to_numpy()[idx_t],
        "is_phantom": True,
    })


def log_ground_truth(events, observed_long):
    out = []
    for ev in events.itertuples(index=False):
        sub = observed_long[
            (observed_long["store_nbr"] == ev.store_nbr) & (observed_long["item_nbr"] == ev.item_nbr)
            & (observed_long["date"] >= ev.start_date) & (observed_long["date"] <= ev.end_date)
        ]
        out.append(dict(
            store_nbr=ev.store_nbr, item_nbr=ev.item_nbr, family=sub["family"].iloc[0],
            week_start=ev.week_start, start_date=ev.start_date, end_date=ev.end_date,
            duration_days=ev.duration_days,
            suppressed_units=sub["unit_sales_true"].sum(),
            on_hand_book_min=sub["on_hand_book"].min(),
            on_hand_book_max=sub["on_hand_book"].max(),
            waste_during_window=sub["waste"].sum(),
        ))
    return pd.DataFrame(out)


def main():
    grid, econ = bf.load_inputs()
    mats = bf.build_matrices(grid, econ)
    fam = bf.family_arrays(mats["store_items"], bf.CALIBRATED_BUFFER)
    dow_table = bf.dow_index_table(grid, econ, fam["fam_list"])

    # baseline: the calibrated, unperturbed simulation -- used only for
    # event selection and as the counterfactual "what would have sold"
    # reference. Not written anywhere new; this is the same run 05_backfill.py
    # already produces.
    receipts0, waste0, emergency0, closing0 = bf.simulate(mats, fam, dow_table, bf.SEED)
    baseline_long = bf.to_long(mats, closing0, receipts0, waste0, emergency0)

    rng = np.random.default_rng(SELECTION_SEED)
    weeks = build_week_universe(baseline_long)
    selected = sample_phantom_weeks(weeks, rng)
    events = choose_events(selected, baseline_long, mats["valid_dates"], rng)

    mask = build_phantom_mask(mats, events)

    # phantom-perturbed run: identical inputs except demand is zeroed inside
    # each event window. Same seed, same buffer -- the lognormal error draws
    # and receipt targets are otherwise unchanged, so every difference in
    # the output is a direct, traceable consequence of the demand mask.
    mats_phantom = dict(mats)
    mats_phantom["S"] = np.where(mask, 0.0, mats["S"])
    receipts1, waste1, emergency1, closing1 = bf.simulate(mats_phantom, fam, dow_table, bf.SEED)
    observed_long = bf.to_long(mats_phantom, closing1, receipts1, waste1, emergency1)
    observed_long = observed_long.rename(columns={"unit_sales": "unit_sales_observed", "on_hand": "on_hand_book"})

    observed_long = observed_long.merge(
        baseline_long[["store_nbr", "item_nbr", "date", "unit_sales"]].rename(columns={"unit_sales": "unit_sales_true"}),
        on=["store_nbr", "item_nbr", "date"], how="left",
    )
    observed_long = observed_long.merge(mask_to_frame(mats, mask), on=["store_nbr", "item_nbr", "date"], how="left")
    # explicit bool cast: after the left-join + fillna, this can otherwise
    # land as an int/object dtype, and "~" on an int Series is bitwise NOT
    # (~0 == -1), not logical negation -- silently turns every downstream
    # ~phantom_rows filter into garbage.
    observed_long["is_phantom"] = observed_long["is_phantom"].fillna(False).astype(bool)

    observed_long.to_parquet(f"{STAGING}/sales_observed.parquet", index=False)
    phantom_events = log_ground_truth(events, observed_long)
    phantom_events.to_parquet(f"{STAGING}/phantom_events.parquet", index=False)

    # --- validation ---
    diag = bf.identity_check(mats_phantom, closing1, receipts1, waste1, emergency1)
    phantom_rows = observed_long["is_phantom"]
    actual_rate = len(events) / len(weeks)

    # fraction of events where on_hand never drops below its value at event start
    starts = observed_long.loc[phantom_rows].sort_values("date").groupby(["store_nbr", "item_nbr"])["on_hand_book"].first()
    mins = observed_long.loc[phantom_rows].groupby(["store_nbr", "item_nbr"])["on_hand_book"].min()
    stair_step_by_item = (mins.reindex(starts.index) >= starts)
    stair_step_rate = stair_step_by_item.mean()

    # by family, for context: this should track shelf_life_days closely --
    # short-shelf-life categories are EXPECTED to show waste-driven dips
    # within the window, not a stair-step bug.
    econ = pd.read_parquet(f"{STAGING}/item_economics.parquet")[["item_nbr", "shelf_life_days"]]
    by_family = (
        stair_step_by_item.reset_index(name="stair_step")
        .merge(phantom_events[["store_nbr", "item_nbr", "family"]].drop_duplicates(), on=["store_nbr", "item_nbr"])
        .merge(econ, on="item_nbr")
        .groupby("family")
        .agg(n=("stair_step", "size"), stair_step_rate=("stair_step", "mean"), shelf_life_days=("shelf_life_days", "first"))
        .sort_values("shelf_life_days")
    )
    print("stair-step rate by family (should track shelf_life_days -- short shelf life -> more waste-driven dips):")
    print(by_family.to_string())
    print()

    baseline_waste_per_day = waste0.sum() / mats["active"].sum()
    event_days = phantom_events["duration_days"].sum()
    event_waste_rate = phantom_events["waste_during_window"].sum() / event_days if event_days else 0.0

    checks = [
        (f"phantom rate ~=0.75% of store-item-week combos (actual {actual_rate:.3%}, n={len(events)}/{len(weeks)})",
         abs(actual_rate - PHANTOM_RATE) < 0.001),
        ("event durations all in [2,6] days", phantom_events["duration_days"].between(MIN_DURATION, MAX_DURATION).all()),
        ("observed sales == 0 for every phantom-flagged row", (observed_long.loc[phantom_rows, "unit_sales_observed"] == 0).all()),
        ("non-phantom-window sales unaffected (observed == true outside any event)",
         (observed_long.loc[~phantom_rows, "unit_sales_observed"] == observed_long.loc[~phantom_rows, "unit_sales_true"]).all()),
        ("on_hand positive at every event's start (real phantom mismatch, not a bookkeeping-correct stockout)",
         (starts > 0).all()),
        (f"on_hand never drops below its event-start value in a majority of events (actual {stair_step_rate:.1%}) "
         "-- the stair-step signature. Bar is 50%, not higher: the by-family breakdown above shows this is "
         "capped by shelf life, not a bug (BAKERY/DELI at shelf_life=2 sit at 28-34%, GROCERY/BEVERAGES at "
         "shelf_life=270-365 sit at 75-79%) -- asserting a higher blended bar would just be asserting a "
         "different item mix than the one this demo actually has",
         stair_step_rate >= 0.50),
        (f"waste rate during event windows exceeds the overall baseline rate "
         f"(event={event_waste_rate:.4f} units/day vs baseline={baseline_waste_per_day:.4f} units/day)",
         event_waste_rate > baseline_waste_per_day),
        ("on_hand never negative", diag["on_hand_min"] >= -1e-3),
        ("accounting identity holds (max err < 0.01 units)", diag["max_abs_error"] < 1e-2),
        ("no duplicate store-item-week phantom picks", not selected.duplicated(["store_nbr", "item_nbr", "week_start"]).any()),
    ]

    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
    assert all(ok for _, ok in checks), "validation gate failed"


if __name__ == "__main__":
    main()
