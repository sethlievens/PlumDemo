"""
Forward simulation: 60 days from 2017-08-16 (the day after history ends),
comparing four ordering policies under IDENTICAL demand. This is the first
script in the project that generates demand rather than replaying it --
Favorita gives no future, so "true demand" has to be modeled, and modeling
it is what makes fact_lost_sales possible at all (see docs note below).

Why this is causal, not inverted, unlike 05_backfill.py:
05_backfill.py inverts the problem because Favorita sales are pre-known
ground truth -- "emergency top-up" there is a bookkeeping fiction that
forces the accounting identity to hold for a past that already happened.
Forward sim has no future to invert against: a shortfall here is a REAL
stockout. units_sold = min(available, true_demand); the gap is a genuine
lost sale, not a same-day magic top-up. That's the whole reason
fact_lost_sales can only be populated here (see sql/01_schema.sql's comment
on that table) -- historical data cannot tell us what didn't happen.

Five scenarios (same true_demand, same phantom events, same RNG seeds --
ONLY the ordering policy differs):
  2. Baseline Forward  -- continues 05_backfill.py's own buffer-based target
     logic (forecast x buffer, capped at velocity x shelf_life), computed
     fresh each day off a live rolling 28-day window of ITS OWN realized
     sales. Does not read par_levels.
  6. Baseline Frozen   -- the SAME buffer formula as Baseline Forward,
     computed ONCE at 2017-08-15 and held frozen for 60 days. Isolates
     whether Baseline's edge over the par-engine scenarios is the FORMULA
     or the ADAPTIVITY -- both this and the three par scenarios below are
     frozen, so any gap between them is formula, not replan frequency.
  3. Optimal Par        -- orders up to par_levels with z DERIVED per item
     from the newsvendor critical ratio (usp_RecommendParLevels), FROZEN at
     the single 2017-08-15 effective_date for the whole 60-day window.
     LIMITATION: a real system re-derives pars weekly as velocity drifts --
     freezing for 60 days UNDERSTATES the engine's benefit here.
  4. Conservative Par  -- same, uniform z=1.65 -- the "textbook default"
     strawman, not economically derived.
  5. Aggressive Par    -- same, uniform z=2.33 -- also a strawman.

Demand model: true_demand = trailing_velocity x dow_index x seasonal_factor
x promo_multiplier x noise.
  - trailing_velocity: a live rolling 28-day mean of true_demand itself,
    seeded from each store-item's real last-28-historical-days of sales.
    Self-referential and IDENTICAL across all 4 scenarios by construction
    (it never reads any scenario's on-hand/sold state) -- this is what
    makes "same seed, only the ordering policy differs" true.
  - dow_index: store-item-level, reused from velocity_daily (scenario 1) --
    NOT the family-level table 05_backfill.py's own ordering forecast uses.
    Two different dow_index tables for two different jobs: this one shapes
    the true, unobserved world; the other shapes what a blind buyer infers
    from trailing sales. Deliberately not the same object.
  - seasonal_factor: a +15% payday bump on the 15th / month-end -- the one
    calendar effect CLAUDE.md documents as real for this data. Nothing
    fancier grafted on.
  - promo_multiplier: pinned to 1.0. No future promo calendar exists in
    this data -- accepted scope cut. Means the Marketing/promo-readiness
    page has nothing to show; only Store Ops (lost sales, waste, service
    level) is in scope for this sim.
  - noise: negative binomial, dispersion r fit PER FAMILY so that, at that
    family's historical mean daily units/store-item, NB's CV matches the
    real historical CV (r = 1 / (CV^2 - 1/mean)). Gate: simulated CV must
    land within +-15% of the historical CV, measured on the UNMASKED
    true_demand draws (pre-phantom) -- phantom's forced zero-days are not
    part of what the demand generator itself is being graded on.

Phantom injection: same 0.75% store-item-week rate and 2-6 day duration as
07_phantom_injection.py, same corrected physics (book on-hand keeps
climbing on cadence, waste ages up), but re-implemented here rather than
imported -- 07's helpers are keyed to the full multi-year lifecycle-aware
historical grid; this is a fixed 60-day panel with no lifecycles, so a
lighter version is clearer than forcing that shape to fit. Selected ONCE
against Baseline Forward's own unperturbed on-hand path (mirrors 07's own
"selection uses the baseline run" principle), then the SAME mask is applied
to true_demand before every one of the four scenario runs.

The mechanic differs from 07 in one respect, and it matters: 07 zeroed
DEMAND (there is no true-vs-observed distinction in a pure reconstruction).
Here, true_demand is untouched during a phantom window -- the customer
still wants to buy -- but the sale can't be RUNG UP (the product is
physically unfindable even though book stock is fine), so units_sold is
forced to 0 and the entire day's true_demand becomes a lost sale. This is
what lets the Store Ops report cleanly separate "lost to under-ordering"
(on_hand genuinely ran out) from "lost to phantom" (on_hand was fine on
paper, the sale still didn't happen) -- see the report at the bottom.

No auto top-up, anywhere, in any scenario: see the causal-vs-inverted note
above.

Vectorized across all ~2,172 store-items with numpy; the only Python-level
loop is over the 60 days (the day-t+1-depends-on-day-t recurrence can't be
avoided, same as 05_backfill.py). par_levels is READ from SQL, never
reimplemented -- one source of truth between this sim and the dashboard.
"""

import importlib.util

import numpy as np
import pandas as pd

spec = importlib.util.spec_from_file_location("backfill", "python/05_backfill.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)

spec2 = importlib.util.spec_from_file_location("load", "python/08_load.py")
ld = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(ld)

STAGING = "data/staging"
FORWARD_START = "2017-08-16"
FORWARD_DAYS = 60
HISTORY_END = "2017-08-15"

DEMAND_SEED = 100          # true-demand NB draws -- shared across all 4 scenarios
BASELINE_FORECAST_SEED = bf.SEED  # Baseline Forward's own lognormal forecast-error noise
SELECTION_SEED = 7         # which store-item-weeks get a phantom event -- matches 07's convention
PHANTOM_RATE = 0.0075
MIN_DURATION, MAX_DURATION = 2, 6
PAYDAY_BUMP = 1.15
CV_GATE_TOLERANCE = 0.15

CARRYING_COST_ANNUAL_RATE = 0.25  # cost of capital + shelf space + shrink risk, applied to unit_cost x avg on-hand

SCENARIO_KEYS = {"Baseline Forward": 2, "Optimal Par": 3, "Conservative Par": 4, "Aggressive Par": 5, "Baseline Frozen": 6}
WEEKDAY_NAME_TO_INT = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6}


# ---------------------------------------------------------------- demand model fit

def fit_nb_dispersion(grid, econ):
    """r = 1 / (CV^2 - 1/mean), solved per family so a negative-binomial draw at
    that family's historical mean daily units/store-item reproduces its historical
    CV. mean and CV computed the same way docs/FINDINGS.md's DELI/BAKERY numbers
    were (mean of per-store-item CV, pooled from the pre-phantom staged grid)."""
    g = grid.merge(econ[["item_nbr", "family"]], on="item_nbr", how="left")
    stats = g.groupby(["store_nbr", "item_nbr", "family"])["unit_sales"].agg(["mean", "std"])
    stats["cv"] = stats["std"] / stats["mean"]
    fam = stats.groupby("family").agg(mean_units=("mean", "mean"), mean_cv=("cv", "mean"))
    fam["r"] = 1.0 / (fam["mean_cv"] ** 2 - 1.0 / fam["mean_units"])
    assert (fam["r"] > 0).all(), f"non-positive NB dispersion fit -- CV too low relative to mean:\n{fam}"
    return fam["mean_units"], fam["mean_cv"], fam["r"]


def seasonal_factor_for_dates(dates):
    is_payday = (dates.day == 15) | dates.is_month_end
    return np.where(is_payday, PAYDAY_BUMP, 1.0).astype(np.float32)


# ---------------------------------------------------------------- SQL reads

def sql_last_28_days_sales(cur, store_items):
    """(28, N) matrix, row 0 = most recent historical day (2017-08-15), row 27 =
    28 days prior. Seeds BOTH the true-demand generator's and Baseline Forward's
    own rolling velocity buffers -- before 2017-08-16 there's no true-vs-observed
    distinction, they're the same ground truth."""
    rows = cur.execute("""
        SELECT dd.date, ds.store_nbr, di.item_nbr, fs.units_sold
        FROM dbo.fact_sales fs
        JOIN dbo.dim_date dd ON dd.date_key = fs.date_key
        JOIN dbo.dim_store ds ON ds.store_key = fs.store_key
        JOIN dbo.dim_item di ON di.item_key = fs.item_key
        WHERE fs.scenario_key = 1 AND dd.date > DATEADD(DAY, -28, ?)
    """, HISTORY_END).fetchall()
    df = pd.DataFrame.from_records(rows, columns=["date", "store_nbr", "item_nbr", "units_sold"])
    df["date"] = pd.to_datetime(df["date"])
    wide = df.pivot(index="date", columns=["store_nbr", "item_nbr"], values="units_sold").fillna(0.0)
    wide = wide.reindex(sorted(wide.index, reverse=True))  # most recent first
    cols = list(zip(store_items["store_nbr"], store_items["item_nbr"]))
    # reindex (not direct []) -- a store-item with zero sales in the last 28
    # calendar days has no rows at all in the pivot, not a column of zeros;
    # missing == legitimately quiet, fill with 0.0 rather than erroring.
    return wide.reindex(columns=cols, fill_value=0.0).to_numpy(dtype=np.float32)


def sql_clean_velocity(cur, store_items):
    """(N,) the last non-NULL trailing_28d_mean_units per store-item as of
    2017-08-15 -- the CLEAN velocity usp_CalculateVelocity already computes
    (excludes promo and stockout days), same value usp_RecommendParLevels'
    own latest_velocity CTE reads. This is the true-demand generator's
    baseline_velocity anchor -- NOT sql_last_28_days_sales' raw mean above.
    Using the raw mean here was a real bug: one GROCERY item was on an
    on_promo_flag=1 promotion for nearly the entire last 28 calendar days of
    history (units_sold up to 2,305/day, vs a clean baseline of 14.4/day),
    so the raw mean anchored true demand on a promotional spike -- directly
    contradicting promo_multiplier=1.0 (no future promo calendar). The clean
    trailing mean already excludes exactly this kind of contamination."""
    rows = cur.execute("""
        SELECT ds.store_nbr, di.item_nbr, v.trailing_28d_mean_units
        FROM (
            SELECT store_key, item_key, trailing_28d_mean_units,
                   ROW_NUMBER() OVER (PARTITION BY store_key, item_key ORDER BY date_key DESC) AS rn
            FROM dbo.velocity_daily
            WHERE scenario_key = 1 AND trailing_28d_mean_units IS NOT NULL
        ) v
        JOIN dbo.dim_store ds ON ds.store_key = v.store_key
        JOIN dbo.dim_item di ON di.item_key = v.item_key
        WHERE v.rn = 1
    """).fetchall()
    df = pd.DataFrame.from_records(rows, columns=["store_nbr", "item_nbr", "trailing_28d_mean_units"])
    aligned = df.set_index(["store_nbr", "item_nbr"])["trailing_28d_mean_units"].reindex(
        pd.MultiIndex.from_frame(store_items[["store_nbr", "item_nbr"]]))
    return aligned.to_numpy(dtype=np.float32)  # NaN where a store-item has no clean history at all


def sql_days_since_last_sale(cur, store_items):
    """(N,) calendar days between each store-item's LAST historical sale and
    2017-08-15. sql_clean_velocity's "last non-NULL trailing_28d_mean_units"
    has no floor on how stale that row is -- an item discontinued in January
    2017 still has a January trailing_28d_mean_units value, and nothing stops
    it from being read as if it were current August demand. Found via: store
    1 / item 298 (a BEVERAGES item) got seeded with a "clean velocity" of
    145.7/day, and turned out to be one of the biggest lost-revenue outliers
    in the whole sim -- its last real sale was 2017-01-15, seven months
    before the sim starts. 144 of 2,175 store-items (6.6%) last sold more
    than 28 days before the effective date; used to drop them from the panel
    entirely rather than simulate demand for discontinued items."""
    rows = cur.execute("""
        SELECT ds.store_nbr, di.item_nbr, MAX(dd.date) AS last_sale_date
        FROM dbo.fact_sales fs
        JOIN dbo.dim_date dd ON dd.date_key = fs.date_key
        JOIN dbo.dim_store ds ON ds.store_key = fs.store_key
        JOIN dbo.dim_item di ON di.item_key = fs.item_key
        WHERE fs.scenario_key = 1
        GROUP BY ds.store_nbr, di.item_nbr
    """).fetchall()
    df = pd.DataFrame.from_records(rows, columns=["store_nbr", "item_nbr", "last_sale_date"])
    df["last_sale_date"] = pd.to_datetime(df["last_sale_date"])
    df["days_since"] = (pd.Timestamp(HISTORY_END) - df["last_sale_date"]).dt.days
    aligned = df.set_index(["store_nbr", "item_nbr"])["days_since"].reindex(
        pd.MultiIndex.from_frame(store_items[["store_nbr", "item_nbr"]]))
    return aligned.to_numpy(dtype=np.float32)


def sql_dow_index(cur, store_items):
    """(N, 7) store-item-level day-of-week multiplier, averaged over all of
    scenario 1's history -- same shape as usp_RecommendParLevels' dow_profile CTE."""
    rows = cur.execute("""
        SELECT ds.store_nbr, di.item_nbr, dd.day_of_week, AVG(v.dow_index) AS avg_dow_index
        FROM dbo.velocity_daily v
        JOIN dbo.dim_date dd ON dd.date_key = v.date_key
        JOIN dbo.dim_store ds ON ds.store_key = v.store_key
        JOIN dbo.dim_item di ON di.item_key = v.item_key
        WHERE v.scenario_key = 1
        GROUP BY ds.store_nbr, di.item_nbr, dd.day_of_week
    """).fetchall()
    df = pd.DataFrame.from_records(rows, columns=["store_nbr", "item_nbr", "day_of_week", "avg_dow_index"])
    df["dow"] = df["day_of_week"].map(WEEKDAY_NAME_TO_INT)
    wide = df.pivot(index=["store_nbr", "item_nbr"], columns="dow", values="avg_dow_index")
    wide = wide.reindex(columns=range(7))
    idx = wide.reindex(pd.MultiIndex.from_frame(store_items[["store_nbr", "item_nbr"]]))
    return idx.fillna(1.0).to_numpy(dtype=np.float32)


def sql_par_levels(cur, store_items):
    """{scenario_key: (N,) frozen par_units array}, aligned to store_items order."""
    rows = cur.execute("""
        SELECT ds.store_nbr, di.item_nbr, pl.scenario_key, pl.par_units
        FROM dbo.par_levels pl
        JOIN dbo.dim_store ds ON ds.store_key = pl.store_key
        JOIN dbo.dim_item di ON di.item_key = pl.item_key
        WHERE pl.scenario_key IN (3, 4, 5)
    """).fetchall()
    df = pd.DataFrame.from_records(rows, columns=["store_nbr", "item_nbr", "scenario_key", "par_units"])
    out = {}
    for sk in (3, 4, 5):
        sub = df.loc[df["scenario_key"] == sk].set_index(["store_nbr", "item_nbr"])["par_units"]
        aligned = sub.reindex(pd.MultiIndex.from_frame(store_items[["store_nbr", "item_nbr"]]))
        out[sk] = aligned.to_numpy(dtype=np.float32)  # NaN where usp_RecommendParLevels had nothing to recommend
    return out


# ---------------------------------------------------------------- true demand generation

def calibrate_nb_dispersion(store_items, fam, clean_velocity, dow_idx, dates, mean_cv, r0, max_iter=8, tol=CV_GATE_TOLERANCE):
    """The closed-form r = 1/(CV^2 - 1/mean) fit only accounts for the NB draw's
    OWN variance -- it silently assumes that's the only source. But dow_index and
    the payday bump are real deterministic multipliers that add variance of their
    own on top, so the realized series CV comes out higher than the closed-form
    predicts (observed +10-32% across families, worse for low-mean families like
    DELI where the 1/mean term is most sensitive to this kind of stacking).
    Proportional-control r per family, in CV^2 (variance) space, same pattern
    06_calibrate.py already uses for the backfill buffer -- converges in a
    handful of iterations rather than needing a second closed form for how much
    variance dow_index/seasonality contribute on their own."""
    families = mean_cv.index.tolist()
    r_by_name = r0.to_dict()
    fam_names_arr = store_items["family"].to_numpy()
    true_demand = sim_cv = None

    for it in range(1, max_iter + 1):
        r_arr = np.array([r_by_name[f] for f in fam_names_arr], dtype=np.float32)
        fam_local = dict(fam)
        fam_local["nb_r"] = r_arr
        rng = np.random.default_rng(DEMAND_SEED)  # same draws each iteration -- only r changes
        true_demand = generate_true_demand(store_items, fam_local, clean_velocity, dow_idx, dates, rng)

        stats = pd.DataFrame(true_demand).agg(["mean", "std"]).T
        stats["family"] = fam_names_arr
        stats["cv"] = stats["std"] / stats["mean"]
        sim_cv = stats.groupby("family")["cv"].mean()

        rel_err = (sim_cv - mean_cv).abs() / mean_cv
        print(f"  iter {it}: " + ", ".join(f"{f}={sim_cv[f]:.3f}(target {mean_cv[f]:.3f})" for f in families))
        if (rel_err <= tol).all():
            print(f"  converged after {it} iteration(s)")
            break

        for f in families:
            inv_r_new = (1.0 / r_by_name[f]) - (sim_cv[f] ** 2 - mean_cv[f] ** 2)
            r_by_name[f] = 1.0 / max(inv_r_new, 1.0 / 200.0)  # r capped at 200 -- near-deterministic floor

    return r_by_name, true_demand, sim_cv


def generate_true_demand(store_items, fam, clean_velocity, dow_idx, dates, rng):
    """expected_demand is anchored to the FIXED, CLEAN day-0 historical velocity
    for the whole window -- day-to-day variation comes from dow_index/seasonal/
    NB noise, but the MEAN itself never moves. Two bugs found and fixed here,
    in the order they were caught:
    (1) An earlier version fed each day's own NB draw back into a rolling
        velocity buffer that became tomorrow's mean -- a self-referential,
        heavy-tailed (r~0.85-1.5) random walk with nothing pulling it back to
        baseline, so a run of bad luck could wander arbitrarily far over 60
        days. Fixed by anchoring to a value that never updates from its own
        output.
    (2) That fixed anchor was then seeded from sql_last_28_days_sales' RAW
        28-day mean -- and one GROCERY item had been on an on_promo_flag=1
        promotion for nearly its entire last 28 days of history (units_sold
        up to 2,305/day vs a clean baseline of 14.4/day), so the raw mean
        anchored true demand on a promotional spike that directly
        contradicted promo_multiplier=1.0 (no future promo calendar). Fixed
        by anchoring to velocity_daily's own CLEAN trailing_28d_mean_units
        (sql_clean_velocity) -- the same promo/stockout-excluded velocity
        usp_RecommendParLevels itself reads, not a re-derived raw one."""
    T, N = FORWARD_DAYS, len(store_items)
    seasonal = seasonal_factor_for_dates(dates)
    weekday = dates.dayofweek.to_numpy()
    r_arr = fam["nb_r"]

    baseline_velocity = clean_velocity  # fixed for the whole window -- never updated from its own draws
    true_demand = np.zeros((T, N), dtype=np.float32)
    for t in range(T):
        dow_mult = dow_idx[np.arange(N), weekday[t]]
        expected = np.maximum(baseline_velocity * dow_mult * seasonal[t], 1e-4)
        p = r_arr / (r_arr + expected)
        true_demand[t] = rng.negative_binomial(r_arr, p).astype(np.float32)
    return true_demand


# ---------------------------------------------------------------- phantom injection

def select_phantom_events(N, T, on_hand_ref, rng):
    n_weeks = -(-T // 7)
    total_combos = N * n_weeks
    k = round(total_combos * PHANTOM_RATE)
    combo_idx = rng.choice(total_combos, size=k, replace=False)
    item_ids, week_ids = combo_idx // n_weeks, combo_idx % n_weeks

    events = []
    for n_i, w_i in zip(item_ids, week_ids):
        day_start, day_end = w_i * 7, min(w_i * 7 + 6, T - 1)
        candidates = [d for d in range(day_start, day_end + 1)
                      if on_hand_ref[d, n_i] > 0 and (T - d) >= MIN_DURATION]
        if not candidates:
            continue
        start = candidates[rng.integers(0, len(candidates))]
        duration = int(rng.integers(MIN_DURATION, MAX_DURATION + 1))
        end = min(start + duration - 1, T - 1)
        events.append((int(n_i), start, end))
    return events


def build_phantom_mask(N, T, events):
    mask = np.zeros((T, N), dtype=bool)
    for n_i, start, end in events:
        mask[start:end + 1, n_i] = True
    return mask


# ---------------------------------------------------------------- scenario physics

def simulate_forward(true_demand, phantom_mask, delivery, shelf_life, cadence, case_pack, target_maker):
    """target_maker(t, opening, vel_buffer_mean) -> (N,) today's order-up-to target.
    Shared FIFO aging/waste/consumption mechanics across all 4 scenarios; only
    target_maker differs. No auto top-up: a shortfall is a real lost sale."""
    T, N = true_demand.shape
    SMAX = int(shelf_life.max()) + 1
    buckets = np.zeros((SMAX, N), dtype=np.float32)
    col_idx = np.arange(N)

    receipts = np.zeros((T, N), dtype=np.float32)
    waste = np.zeros((T, N), dtype=np.float32)
    sold = np.zeros((T, N), dtype=np.float32)
    lost = np.zeros((T, N), dtype=np.float32)
    closing = np.zeros((T, N), dtype=np.float32)

    for t in range(T):
        buckets = np.roll(buckets, 1, axis=0)
        buckets[0, :] = 0.0

        waste_today = buckets[shelf_life, col_idx].copy()
        buckets[shelf_life, col_idx] = 0.0

        opening = buckets.sum(axis=0)
        target = target_maker(t, opening)
        receipt_needed = np.maximum(0.0, target - opening)
        receipt_qty = np.where(delivery[t], np.ceil(receipt_needed / case_pack) * case_pack, 0.0)
        buckets[0, :] += receipt_qty

        available = buckets.sum(axis=0)
        required = true_demand[t]
        fulfillable = np.where(phantom_mask[t], 0.0, available)  # physically unfindable during a phantom window
        sold_today = np.minimum(fulfillable, required)
        lost_today = np.maximum(0.0, required - sold_today)

        reversed_buckets = buckets[::-1, :]
        cum = np.cumsum(reversed_buckets, axis=0)
        prior = np.vstack([np.zeros((1, N), dtype=np.float32), cum[:-1, :]])
        removed = np.clip(sold_today[None, :] - prior, 0.0, reversed_buckets)
        buckets = (reversed_buckets - removed)[::-1, :]

        receipts[t], waste[t], sold[t], lost[t], closing[t] = receipt_qty, waste_today, sold_today, lost_today, buckets.sum(axis=0)

    return dict(receipts=receipts, waste=waste, sold=sold, lost=lost, closing=closing)


def make_baseline_target_fn(fam, vel_seed, dow_family_table, dates, seed):
    """Baseline Forward: identical formula to 05_backfill.simulate(), except
    velocity is a LIVE rolling mean of this scenario's own realized sales
    (seeded from real history), not a precomputed matrix -- there is no future
    to precompute from."""
    rng = np.random.default_rng(seed)
    weekday = dates.dayofweek.to_numpy()
    vel_buffer = vel_seed.copy()
    state = {"vel_buffer": vel_buffer}

    def target_fn(t, opening):
        velocity_t = state["vel_buffer"].mean(axis=0)
        dow_idx_t = dow_family_table[fam["fam_idx"], weekday[t]]
        error = rng.lognormal(mean=fam["mu"], sigma=fam["sigma"])
        forecast = velocity_t * dow_idx_t * fam["cadence"] * error
        return np.minimum(forecast * fam["buffer"], velocity_t * fam["shelf_life"])

    def after_day(sold_today):
        state["vel_buffer"] = np.roll(state["vel_buffer"], 1, axis=0)
        state["vel_buffer"][0, :] = sold_today

    return target_fn, after_day


def make_baseline_frozen_target(fam, vel_seed, dow_family_table, dates):
    """Baseline Frozen: the SAME buffer formula as Baseline Forward, but computed
    ONCE at t=0 and held constant for the whole 60 days -- isolates whether the
    par engine's disadvantage vs Baseline Forward is the FORMULA (buffer vs
    z-safety-stock) or the ADAPTIVITY (daily replan vs frozen). error=1 (no
    lognormal draw) -- a frozen plan is a point estimate, not a resampled one
    each day; Baseline Forward keeps its own stochastic term since it genuinely
    re-forecasts daily."""
    velocity_0 = vel_seed.mean(axis=0)
    weekday_0 = dates[0].dayofweek
    dow_idx_0 = dow_family_table[fam["fam_idx"], weekday_0]
    forecast = velocity_0 * dow_idx_0 * fam["cadence"]
    return np.minimum(forecast * fam["buffer"], velocity_0 * fam["shelf_life"])


def run_baseline(true_demand, phantom_mask, delivery, fam, vel_seed, dow_family_table, dates):
    """Baseline needs its rolling velocity updated with EACH day's realized sold
    units after that day resolves, so it can't use the generic simulate_forward
    target_maker signature unchanged -- wrap it with a day-synchronous callback."""
    T, N = true_demand.shape
    target_fn, after_day = make_baseline_target_fn(fam, vel_seed, dow_family_table, dates, BASELINE_FORECAST_SEED)

    SMAX = int(fam["shelf_life"].max()) + 1
    buckets = np.zeros((SMAX, N), dtype=np.float32)
    col_idx = np.arange(N)
    shelf_life, cadence, case_pack = fam["shelf_life"], fam["cadence"], fam["case_pack"]

    receipts = np.zeros((T, N), dtype=np.float32)
    waste = np.zeros((T, N), dtype=np.float32)
    sold = np.zeros((T, N), dtype=np.float32)
    lost = np.zeros((T, N), dtype=np.float32)
    closing = np.zeros((T, N), dtype=np.float32)

    for t in range(T):
        buckets = np.roll(buckets, 1, axis=0)
        buckets[0, :] = 0.0
        waste_today = buckets[shelf_life, col_idx].copy()
        buckets[shelf_life, col_idx] = 0.0

        opening = buckets.sum(axis=0)
        target = target_fn(t, opening)
        receipt_needed = np.maximum(0.0, target - opening)
        receipt_qty = np.where(delivery[t], np.ceil(receipt_needed / case_pack) * case_pack, 0.0)
        buckets[0, :] += receipt_qty

        available = buckets.sum(axis=0)
        required = true_demand[t]
        fulfillable = np.where(phantom_mask[t], 0.0, available)
        sold_today = np.minimum(fulfillable, required)
        lost_today = np.maximum(0.0, required - sold_today)

        reversed_buckets = buckets[::-1, :]
        cum = np.cumsum(reversed_buckets, axis=0)
        prior = np.vstack([np.zeros((1, N), dtype=np.float32), cum[:-1, :]])
        removed = np.clip(sold_today[None, :] - prior, 0.0, reversed_buckets)
        buckets = (reversed_buckets - removed)[::-1, :]

        receipts[t], waste[t], sold[t], lost[t], closing[t] = receipt_qty, waste_today, sold_today, lost_today, buckets.sum(axis=0)
        after_day(sold_today)

    return dict(receipts=receipts, waste=waste, sold=sold, lost=lost, closing=closing)


# ---------------------------------------------------------------- output shaping / load

def to_long(store_items, dates, result, scenario_key):
    T, N = result["sold"].shape
    idx_t, idx_n = np.meshgrid(np.arange(T), np.arange(N), indexing="ij")
    idx_t, idx_n = idx_t.ravel(), idx_n.ravel()
    return pd.DataFrame({
        "store_nbr": store_items["store_nbr"].to_numpy()[idx_n],
        "item_nbr": store_items["item_nbr"].to_numpy()[idx_n],
        "date": dates.to_numpy()[idx_t],
        "scenario_key": scenario_key,
        "true_demand": result["true_demand"][idx_t, idx_n] if "true_demand" in result else np.nan,
        "units_sold": result["sold"][idx_t, idx_n],
        "receipts": result["receipts"][idx_t, idx_n],
        "waste": result["waste"][idx_t, idx_n],
        "lost_units": result["lost"][idx_t, idx_n],
        "on_hand": result["closing"][idx_t, idx_n],
    })


def main():
    print("=== phase 1: load inputs ===")
    grid, econ = bf.load_inputs()
    mats = bf.build_matrices(grid, econ)
    store_items = mats["store_items"]
    fam = bf.family_arrays(store_items, bf.CALIBRATED_BUFFER)
    dow_family_table = bf.dow_index_table(grid, econ, fam["fam_list"])

    mean_units, mean_cv, nb_r = fit_nb_dispersion(grid, econ)
    fam_r_by_name = nb_r.to_dict()
    fam["nb_r"] = np.array([fam_r_by_name[f] for f in store_items["family"]], dtype=np.float32)
    print("NB dispersion fit (r = 1/(CV^2 - 1/mean)):")
    print(pd.DataFrame({"mean_units": mean_units, "historical_cv": mean_cv, "r": nb_r}).round(3).to_string())

    conn = ld.get_conn()
    cur = conn.cursor()
    vel_seed = sql_last_28_days_sales(cur, store_items)          # RAW -- seeds Baseline's OWN rolling velocity only
    clean_velocity = sql_clean_velocity(cur, store_items)         # CLEAN (promo/stockout-excluded) -- true-demand anchor
    days_since_last_sale = sql_days_since_last_sale(cur, store_items)
    dow_idx_store_item = sql_dow_index(cur, store_items)
    par_by_scenario = sql_par_levels(cur, store_items)

    # Drop store-items that shouldn't be forward-simulated at all:
    #  - no usable history at all (a single, NULL, velocity_daily row --
    #    usp_RecommendParLevels correctly has nothing to recommend for them)
    #  - DISCONTINUED as of the effective date: last real sale more than 28
    #    days before 2017-08-15 (144 of 2,175 store-items, 6.6%). Their
    #    "clean velocity" is not stale-in-the-usual-sense -- it's the last
    #    value from whenever they were still active, which can be months
    #    old and have no relationship to current demand. Found via: store 1
    #    / item 298 (BEVERAGES) seeded at 145.7/day from a January 2017
    #    snapshot -- its last real sale was 7 months before the sim starts --
    #    and became one of the largest lost-revenue outliers in the sim.
    # Same exclusion for every scenario, so the comparison stays apples-to-apples.
    keep = ~np.isnan(par_by_scenario[3]) & ~np.isnan(clean_velocity) & (days_since_last_sale <= 28)
    n_dropped = int((~keep).sum())
    if n_dropped:
        print(f"dropping {n_dropped} store-item(s): no usable history, or last sale >28 days before effective date")
        store_items = store_items.loc[keep].reset_index(drop=True)
        for k in fam:
            if isinstance(fam[k], np.ndarray):
                fam[k] = fam[k][keep]
        vel_seed = vel_seed[:, keep]
        clean_velocity = clean_velocity[keep]
        dow_idx_store_item = dow_idx_store_item[keep]
        par_by_scenario = {sk: arr[keep] for sk, arr in par_by_scenario.items()}

    dates = pd.date_range(FORWARD_START, periods=FORWARD_DAYS, freq="D")
    T, N = FORWARD_DAYS, len(store_items)
    print(f"store-items: {N}, forward days: {T} ({dates[0].date()} .. {dates[-1].date()})")

    print("\n=== phase 2: generate true demand (shared across all scenarios) ===")
    r_calibrated, true_demand, sim_cv_by_family = calibrate_nb_dispersion(
        store_items, fam, clean_velocity, dow_idx_store_item, dates, mean_cv, nb_r)

    cv_check = pd.DataFrame({"historical_cv": mean_cv, "simulated_cv": sim_cv_by_family, "calibrated_r": pd.Series(r_calibrated)})
    cv_check["rel_err"] = (cv_check["simulated_cv"] - cv_check["historical_cv"]).abs() / cv_check["historical_cv"]
    print(cv_check.round(3).to_string())

    cv_gate_ok = (cv_check["rel_err"] <= CV_GATE_TOLERANCE).all()
    print(f"{'PASS' if cv_gate_ok else 'FAIL'}: simulated CV within +-{CV_GATE_TOLERANCE:.0%} of historical CV, all families")
    assert cv_gate_ok, "demand model CV gate failed -- do not trust the scenario comparison below"

    print("\n=== phase 3: select phantom events (against Baseline's own unperturbed path) ===")
    delivery = (np.arange(T)[:, None] % fam["cadence"][None, :].astype(np.int64)) == 0
    no_phantom = np.zeros((T, N), dtype=bool)
    ref = run_baseline(true_demand, no_phantom, delivery, fam, vel_seed, dow_family_table, dates)

    selection_rng = np.random.default_rng(SELECTION_SEED)
    events = select_phantom_events(N, T, ref["closing"], selection_rng)
    phantom_mask = build_phantom_mask(N, T, events)
    n_weeks = -(-T // 7)
    actual_rate = len(events) / (N * n_weeks)
    print(f"{'PASS' if abs(actual_rate - PHANTOM_RATE) < 0.001 else 'FAIL'}: "
          f"phantom rate ~=0.75% of store-item-weeks (actual {actual_rate:.3%}, n={len(events)})")

    print("\n=== phase 4: run all 5 scenarios ===")
    results = {}
    results["Baseline Forward"] = run_baseline(true_demand, phantom_mask, delivery, fam, vel_seed, dow_family_table, dates)

    frozen_target = make_baseline_frozen_target(fam, vel_seed, dow_family_table, dates)
    results["Baseline Frozen"] = simulate_forward(true_demand, phantom_mask, delivery, fam["shelf_life"], fam["cadence"],
                                                    fam["case_pack"], target_maker=lambda t, opening, ft=frozen_target: ft)

    for name, sk in (("Optimal Par", 3), ("Conservative Par", 4), ("Aggressive Par", 5)):
        par_target = par_by_scenario[sk]
        results[name] = simulate_forward(true_demand, phantom_mask, delivery, fam["shelf_life"], fam["cadence"],
                                          fam["case_pack"], target_maker=lambda t, opening, pt=par_target: pt)

    print("\n=== phase 5: validate + write to SQL ===")
    econ_lookup = econ.set_index("item_nbr")

    store_key = dict(cur.execute("SELECT store_nbr, store_key FROM dbo.dim_store").fetchall())
    item_key = dict(cur.execute("SELECT item_nbr, item_key FROM dbo.dim_item").fetchall())
    vendor_key = dict(cur.execute("SELECT vendor_name, vendor_key FROM dbo.dim_vendor").fetchall())

    all_sales, all_receipts, all_waste, all_lost, all_inv = [], [], [], [], []
    report_rows = []

    for name, res in results.items():
        sk = SCENARIO_KEYS[name]
        neg_on_hand = res["closing"].min()
        assert neg_on_hand >= -1e-3, f"{name}: on_hand went negative ({neg_on_hand})"

        long = to_long(store_items, dates, {**res, "true_demand": true_demand}, sk)
        long = long.merge(store_items[["store_nbr", "item_nbr", "family"]], on=["store_nbr", "item_nbr"], how="left")
        long["unit_price"] = long["item_nbr"].map(econ_lookup["unit_price"])
        long["unit_cost"] = long["item_nbr"].map(econ_lookup["unit_cost"])
        long["store_key"] = long["store_nbr"].map(store_key)
        long["item_key"] = long["item_nbr"].map(item_key)
        long["date_key"] = long["date"].dt.strftime("%Y%m%d").astype(int)

        # fact_sales -- full panel
        fs = long.copy()
        fs["gross_revenue"] = (fs["units_sold"] * fs["unit_price"]).round(2)
        fs["cogs"] = (fs["units_sold"] * fs["unit_cost"]).round(2)
        all_sales.append(fs[["date_key", "store_key", "item_key", "scenario_key", "units_sold", "gross_revenue", "cogs"]]
                          .assign(units_sold=lambda d: d["units_sold"].round(2), on_promo_flag=0))

        # fact_inventory_snap -- full panel
        mean_sold_by_item = fs.groupby(["store_nbr", "item_nbr"])["units_sold"].transform("mean")
        inv = long.copy()
        inv["on_hand_units"] = inv["on_hand"].round(2)
        inv["on_hand_value"] = (inv["on_hand_units"] * inv["unit_cost"]).round(2)
        inv["days_of_supply"] = np.where(mean_sold_by_item > 0, (inv["on_hand_units"] / mean_sold_by_item).round(2), np.nan)
        all_inv.append(inv[["date_key", "store_key", "item_key", "scenario_key", "on_hand_units", "on_hand_value", "days_of_supply"]])

        # fact_receipts -- delivery days only
        rc = long.loc[long["receipts"] > 0].copy()
        rc["vendor_key"] = rc["family"].map(lambda f: vendor_key[ld.VENDOR_BY_FAMILY[f]])
        rc["expiry_date"] = (rc["date"] + pd.to_timedelta(rc["item_nbr"].map(econ_lookup["shelf_life_days"]), unit="D")).dt.strftime("%Y-%m-%d")
        rc["ordered_units"] = rc["received_units"] = rc["receipts"].round(2)
        all_receipts.append(rc[["date_key", "store_key", "item_key", "vendor_key", "scenario_key",
                                 "ordered_units", "received_units", "unit_cost", "expiry_date"]]
                             .assign(is_emergency_topup=0))

        # fact_waste -- waste days only
        ws = long.loc[long["waste"] > 0].copy()
        ws["units_wasted"] = ws["waste"].round(2)
        ws["waste_cost"] = (ws["units_wasted"] * ws["unit_cost"]).round(2)
        all_waste.append(ws[["date_key", "store_key", "item_key", "scenario_key", "units_wasted", "waste_cost"]].assign(reason="Expired"))

        # fact_lost_sales -- shortfall days only
        ls = long.loc[long["lost_units"] > 1e-6].copy()
        ls["true_demand_units"] = ls["true_demand"].round(2)
        ls["units_sold_r"] = ls["units_sold"].round(2)
        ls["lost_units_r"] = ls["lost_units"].round(2)
        ls["lost_revenue"] = (ls["lost_units"] * ls["unit_price"]).round(2)
        all_lost.append(ls[["date_key", "store_key", "item_key", "scenario_key",
                             "true_demand_units", "units_sold_r", "lost_units_r", "lost_revenue"]]
                         .rename(columns={"units_sold_r": "units_sold", "lost_units_r": "lost_units"}))

        # --- report, by family ---
        by_fam = long.groupby("family").agg(
            true_demand=("true_demand", "sum"), units_sold=("units_sold", "sum"),
            lost_units=("lost_units", "sum"), waste=("waste", "sum"),
        )
        by_fam["revenue"] = long.groupby("family").apply(lambda d: (d["units_sold"] * d["unit_price"]).sum(), include_groups=False)
        by_fam["waste_cost"] = long.groupby("family").apply(lambda d: (d["waste"] * d["unit_cost"]).sum(), include_groups=False)
        by_fam["lost_revenue"] = long.groupby("family").apply(lambda d: (d["lost_units"] * d["unit_price"]).sum(), include_groups=False)
        # carrying cost: ~25%/year cost of capital + shelf space + shrink risk,
        # charged daily against on-hand VALUE (not units) -- this is what makes
        # "just carry more stock" stop being free for long-shelf-life families
        # where nothing expires inside the 60-day window.
        by_fam["carrying_cost"] = long.groupby("family").apply(
            lambda d: (d["on_hand"] * d["unit_cost"]).sum() * CARRYING_COST_ANNUAL_RATE / 365, include_groups=False)
        by_fam["service_level"] = 1 - by_fam["lost_units"] / by_fam["true_demand"]
        by_fam["scenario"] = name
        report_rows.append(by_fam.reset_index())

    sales_df = pd.concat(all_sales, ignore_index=True)
    inv_df = pd.concat(all_inv, ignore_index=True)
    receipts_df = pd.concat(all_receipts, ignore_index=True)
    waste_df = pd.concat(all_waste, ignore_index=True)
    lost_df = pd.concat(all_lost, ignore_index=True)

    ld.write_csv(sales_df, "fact_sales_fwd.csv")
    ld.write_csv(inv_df, "fact_inventory_snap_fwd.csv")
    ld.write_csv(receipts_df, "fact_receipts_fwd.csv")
    ld.write_csv(waste_df, "fact_waste_fwd.csv")
    ld.write_csv(lost_df, "fact_lost_sales_fwd.csv")
    ld.sync_csv_dir_to_container()

    # re-running this script must REPLACE the forward-sim scenarios, not
    # duplicate them -- BULK INSERT only appends, so clear scenarios 2-6 first.
    for t in ("fact_sales", "fact_inventory_snap", "fact_receipts", "fact_waste", "fact_lost_sales"):
        cur.execute(f"DELETE FROM dbo.{t} WHERE scenario_key IN (2,3,4,5,6)")
    conn.commit()

    ld.bulk_insert(cur, "v_load_fact_sales", "fact_sales_fwd.csv")
    ld.bulk_insert(cur, "v_load_fact_inventory_snap", "fact_inventory_snap_fwd.csv")
    ld.bulk_insert(cur, "v_load_fact_receipts", "fact_receipts_fwd.csv")
    ld.bulk_insert(cur, "v_load_fact_waste", "fact_waste_fwd.csv")
    ld.bulk_insert(cur, "v_load_fact_lost_sales", "fact_lost_sales_fwd.csv")
    conn.commit()

    counts = {t: cur.execute(f"SELECT COUNT(*) FROM dbo.{t} WHERE scenario_key IN (2,3,4,5,6)").fetchone()[0]
              for t in ("fact_sales", "fact_inventory_snap", "fact_receipts", "fact_waste", "fact_lost_sales")}
    print("rows written (scenarios 2-6):", counts)
    conn.close()

    print("\n=== phase 6: scenario comparison, by family ===")
    report = pd.concat(report_rows, ignore_index=True)
    report = report[["scenario", "family", "revenue", "lost_revenue", "waste_cost", "carrying_cost", "service_level"]]
    for col in ("revenue", "lost_revenue", "waste_cost", "carrying_cost"):
        report[col] = report[col].round(0).astype(int)
    report["service_level"] = (report["service_level"] * 100).round(1)
    report["net"] = report["revenue"] - report["lost_revenue"] - report["waste_cost"] - report["carrying_cost"]
    order = ["Baseline Forward", "Baseline Frozen", "Optimal Par", "Conservative Par", "Aggressive Par"]
    report["scenario"] = pd.Categorical(report["scenario"], order, ordered=True)
    print(report.sort_values(["family", "scenario"]).to_string(index=False))

    print("\n=== totals by scenario (net = revenue - lost_revenue - waste_cost - carrying_cost) ===")
    totals = report.groupby("scenario", observed=True).agg(
        revenue=("revenue", "sum"), lost_revenue=("lost_revenue", "sum"),
        waste_cost=("waste_cost", "sum"), carrying_cost=("carrying_cost", "sum"), net=("net", "sum"),
    )
    totals["service_level"] = report.groupby("scenario", observed=True).apply(
        lambda d: 100 * (1 - d["lost_revenue"].sum() / (d["revenue"].sum() + d["lost_revenue"].sum())), include_groups=False
    ).round(1)
    print(totals.to_string())


if __name__ == "__main__":
    main()
