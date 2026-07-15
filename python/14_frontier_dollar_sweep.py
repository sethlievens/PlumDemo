"""
Dollar-denominated ordering-cost frontier from the forward sim, for DELI
and DAIRY -- supersedes the DELI-only 14_frontier_deli_sweep.py. Same
architecture: 12_simulate.py's demand/phantom generation runs ONCE
(imported, unchanged -- it's the expensive shared part), then each sweep
point only calls usp_RecommendParLevels into a scratch scenario and re-runs
simulate_forward on that family's slice of the shared arrays.

Objective (Total Cost of Ordering, per the corrected accounting):
    spoilage $        = units_wasted x unit_cost
    missed sales $    = lost_units x (retail_price - unit_cost)  [MARGIN, not retail]
    carrying $        = 25%/yr on daily closing on-hand value
    total $           = all three

THE SWEEP GOES NEGATIVE for DELI. z=0 won the first (z >= 0) sweep at the
boundary -- and a boundary minimum means the search interval was wrong, not
that the answer is the boundary. Negative z = par below expected demand =
deliberate under-production, the textbook newsvendor prescription whenever
the critical ratio < 0.5 (overstock cost exceeds understock cost). DELI's
MODELED ratio is 0.573, but the first sweep showed the sim's realized
spoilage cost running well above the model's exponential P(expire)
approximation -- enough to push the TRUE ratio under 0.5, hence the
interior minimum is expected at negative z.

EMPIRICAL CRITICAL RATIO: after each family's sweep, the discrete minimum
(parabola-fit through the three points around it, in z space) gives an
empirical z*; Phi(z*) is the critical ratio the sim actually rewards, and
Co_implied = Cu x (1 - Phi(z*)) / Phi(z*) backs out the overstock cost the
sim actually charges. Compared against the same numbers backed out of the
proc's persisted critical_ratio -- the gap is a real, reportable model
error in usp_RecommendParLevels' exponential P(expire) term.
"""

import importlib.util
import math

import numpy as np
import pandas as pd


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

bf = _load_module("backfill", "python/05_backfill.py")
ld = _load_module("load", "python/08_load.py")
sim = _load_module("simulate", "python/12_simulate.py")

SCRATCH_SCENARIO_KEY = 7   # dim_scenario 'DELI Sweep (scratch)' -- reused for any family's sweep
SOURCE_SCENARIO_KEY = 1    # Historical
ENGINE_SCENARIO_KEY = 3    # Engine Recommended -- already-simulated, real point to mark
DDL_FILE = "sql/08_create_frontier_dollar.sql"

# Sweep grids, chosen so the expected optimum sits in the INTERIOR:
#   DELI:  first sweep's boundary minimum at z=0 + the under-0.5 true-ratio
#          argument above -> extend well below zero.
#   DAIRY: modeled z is 0.82 with real margin AND real spoilage -> classic
#          crossing-curves territory; bracket generously on both sides.
FAMILY_Z_GRIDS = {
    "DELI":  [round(-1.5 + 0.25 * i, 2) for i in range(11)],  # -1.5 .. +1.0
    "DAIRY": [round(-0.5 + 0.25 * i, 2) for i in range(11)],  # -0.5 .. +2.0
}


def norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def run_par_sweep_point(conn, cur, family, z):
    """Writes par_levels for the scratch scenario at this family+z, returns
    {(store_nbr, item_nbr): (par_units, days_of_safety_stock)}."""
    cur.execute(
        "EXEC dbo.usp_RecommendParLevels @source_scenario_key=?, @output_scenario_key=?, "
        "@family_filter=?, @override_z=?",
        SOURCE_SCENARIO_KEY, SCRATCH_SCENARIO_KEY, family, z,
    )
    conn.commit()
    rows = cur.execute("""
        SELECT ds.store_nbr, di.item_nbr, pl.par_units, pl.days_of_safety_stock
        FROM dbo.par_levels pl
        JOIN dbo.dim_store ds ON ds.store_key = pl.store_key
        JOIN dbo.dim_item di ON di.item_key = pl.item_key
        WHERE pl.scenario_key = ?
    """, SCRATCH_SCENARIO_KEY).fetchall()
    return {(r[0], r[1]): (r[2], r[3]) for r in rows}


def engine_point(cur, family):
    """The real, already-simulated Engine Recommended costs for this family.
    Three independent aggregates, NOT one multi-join -- par_levels is keyed
    store x item, and joining fact tables to it on item_key alone fans out
    6x (caught the hard way in the first version of this script)."""
    days = cur.execute("""
        SELECT AVG(CAST(pl.days_of_safety_stock AS FLOAT))
        FROM dbo.par_levels pl JOIN dbo.dim_item di ON di.item_key = pl.item_key
        WHERE pl.scenario_key = ? AND di.family = ?
    """, ENGINE_SCENARIO_KEY, family).fetchone()[0]
    spoil = cur.execute("""
        SELECT SUM(CAST(w.units_wasted AS FLOAT) * di.unit_cost)
        FROM dbo.fact_waste w JOIN dbo.dim_item di ON di.item_key = w.item_key
        WHERE w.scenario_key = ? AND di.family = ?
    """, ENGINE_SCENARIO_KEY, family).fetchone()[0]
    missed = cur.execute("""
        SELECT SUM(CAST(ls.lost_units AS FLOAT) * (di.retail_price - di.unit_cost))
        FROM dbo.fact_lost_sales ls JOIN dbo.dim_item di ON di.item_key = ls.item_key
        WHERE ls.scenario_key = ? AND di.family = ?
    """, ENGINE_SCENARIO_KEY, family).fetchone()[0]
    carrying = cur.execute("""
        SELECT SUM(CAST(inv.on_hand_value AS FLOAT)) * ? / 365
        FROM dbo.fact_inventory_snap inv JOIN dbo.dim_item di ON di.item_key = inv.item_key
        WHERE inv.scenario_key = ? AND di.family = ?
    """, sim.CARRYING_COST_ANNUAL_RATE, ENGINE_SCENARIO_KEY, family).fetchone()[0]
    return float(days), float(spoil or 0), float(missed or 0), float(carrying or 0)


def empirical_critical_ratio(swept, family, cur):
    """Parabola-fit z* around the discrete total-cost minimum -> empirical
    critical ratio Phi(z*) -> implied Co. Compared to the proc's own
    persisted (modeled) critical_ratio for the same family."""
    idx = swept["total_dollars"].idxmin()
    z_min = float(swept.loc[idx, "z"])
    interior = 0 < idx < len(swept) - 1

    if interior:
        z3 = swept.loc[idx - 1:idx + 1, "z"].astype(float).to_numpy()
        y3 = swept.loc[idx - 1:idx + 1, "total_dollars"].astype(float).to_numpy()
        a, b, _c = np.polyfit(z3, y3, 2)
        z_star = float(-b / (2 * a)) if a > 0 else z_min
    else:
        z_star = z_min  # boundary -- no interior vertex to fit

    ratio_emp = norm_cdf(z_star)

    cu_avg, ratio_modeled = cur.execute("""
        SELECT AVG(di.retail_price - di.unit_cost), AVG(CAST(pl.critical_ratio AS FLOAT))
        FROM dbo.par_levels pl JOIN dbo.dim_item di ON di.item_key = pl.item_key
        WHERE pl.scenario_key = ? AND di.family = ?
    """, ENGINE_SCENARIO_KEY, family).fetchone()
    cu_avg, ratio_modeled = float(cu_avg), float(ratio_modeled)

    co_implied = cu_avg * (1.0 - ratio_emp) / ratio_emp if ratio_emp > 0 else float("inf")
    co_modeled = cu_avg * (1.0 - ratio_modeled) / ratio_modeled

    return dict(family=family, interior=interior, z_star=z_star,
                ratio_empirical=ratio_emp, ratio_modeled=ratio_modeled,
                cu_avg=cu_avg, co_implied=co_implied, co_modeled=co_modeled)


def main():
    print("=== setup: shared demand/phantom generation (one-time cost) ===")
    grid, econ = bf.load_inputs()
    mats = bf.build_matrices(grid, econ)
    store_items = mats["store_items"]
    fam = bf.family_arrays(store_items, bf.CALIBRATED_BUFFER)
    dow_family_table = bf.dow_index_table(grid, econ, fam["fam_list"])

    mean_units, mean_cv, nb_r = sim.fit_nb_dispersion(grid, econ)
    fam_r_by_name = nb_r.to_dict()
    fam["nb_r"] = np.array([fam_r_by_name[f] for f in store_items["family"]], dtype=np.float32)

    conn = ld.get_conn()
    cur = conn.cursor()
    vel_seed = sim.sql_last_28_days_sales(cur, store_items)
    clean_velocity = sim.sql_clean_velocity(cur, store_items)
    days_since_last_sale = sim.sql_days_since_last_sale(cur, store_items)
    dow_idx_store_item = sim.sql_dow_index(cur, store_items)
    par_by_scenario = sim.sql_par_levels(cur, store_items)

    # same drop criteria as 12_simulate.main() -- identical surviving universe
    keep = ~np.isnan(par_by_scenario[3]) & ~np.isnan(clean_velocity) & (days_since_last_sale <= 28)
    store_items = store_items.loc[keep].reset_index(drop=True)
    for k in fam:
        if isinstance(fam[k], np.ndarray):
            fam[k] = fam[k][keep]
    vel_seed = vel_seed[:, keep]
    clean_velocity = clean_velocity[keep]
    dow_idx_store_item = dow_idx_store_item[keep]

    dates = pd.date_range(sim.FORWARD_START, periods=sim.FORWARD_DAYS, freq="D")
    T, N = sim.FORWARD_DAYS, len(store_items)
    print(f"store-items (all families, post-drop): {N}")

    r_calibrated, true_demand, sim_cv_by_family = sim.calibrate_nb_dispersion(
        store_items, fam, clean_velocity, dow_idx_store_item, dates, mean_cv, nb_r)

    delivery = (np.arange(T)[:, None] % fam["cadence"][None, :].astype(np.int64)) == 0
    no_phantom = np.zeros((T, N), dtype=bool)
    ref = sim.run_baseline(true_demand, no_phantom, delivery, fam, vel_seed, dow_family_table, dates)
    selection_rng = np.random.default_rng(sim.SELECTION_SEED)
    events = sim.select_phantom_events(N, T, ref["closing"], selection_rng)
    phantom_mask = sim.build_phantom_mask(N, T, events)

    econ_lookup = econ.set_index("item_nbr")
    frontier_rows = []
    analyses = []

    for family, z_grid in FAMILY_Z_GRIDS.items():
        print(f"\n=== {family}: sweeping z over {z_grid} ===")
        fmask = (store_items["family"] == family).to_numpy()
        f_items = store_items.loc[fmask].reset_index(drop=True)
        td_f = true_demand[:, fmask]
        pm_f = phantom_mask[:, fmask]
        dl_f = delivery[:, fmask]
        shelf_f, cad_f, cp_f = fam["shelf_life"][fmask], fam["cadence"][fmask], fam["case_pack"][fmask]
        cost_f = f_items["item_nbr"].map(econ_lookup["unit_cost"]).to_numpy(dtype=np.float32)
        price_f = f_items["item_nbr"].map(econ_lookup["unit_price"]).to_numpy(dtype=np.float32)
        margin_f = price_f - cost_f
        keys = list(zip(f_items["store_nbr"], f_items["item_nbr"]))
        print(f"{family} store-items: {len(f_items)}")

        for z in z_grid:
            par_by_key = run_par_sweep_point(conn, cur, family, z)
            par_units = np.array([par_by_key[k][0] for k in keys], dtype=np.float32)
            days_safety = np.array([par_by_key[k][1] for k in keys], dtype=np.float32)

            res = sim.simulate_forward(td_f, pm_f, dl_f, shelf_f, cad_f, cp_f,
                                        target_maker=lambda t, opening, pt=par_units: pt)

            spoil = float((res["waste"] * cost_f[None, :]).sum())
            missed = float((res["lost"] * margin_f[None, :]).sum())
            carrying = float((res["closing"] * cost_f[None, :]).sum() * sim.CARRYING_COST_ANNUAL_RATE / 365)
            total = spoil + missed + carrying
            avg_days = float(np.nanmean(days_safety))

            frontier_rows.append(dict(
                family=family, z=z, days_of_safety_stock=avg_days,
                spoilage_dollars=round(spoil, 2), missed_sales_dollars=round(missed, 2),
                carrying_dollars=round(carrying, 2), total_dollars=round(total, 2),
                is_engine_point=0,
            ))
            print(f"  z={z:+.2f}  days={avg_days:+.3f}  spoilage=${spoil:,.0f}  "
                  f"missed=${missed:,.0f}  carrying=${carrying:,.0f}  total=${total:,.0f}")

        e_days, e_spoil, e_missed, e_carry = engine_point(cur, family)
        e_total = e_spoil + e_missed + e_carry
        frontier_rows.append(dict(
            family=family, z=None, days_of_safety_stock=e_days,
            spoilage_dollars=round(e_spoil, 2), missed_sales_dollars=round(e_missed, 2),
            carrying_dollars=round(e_carry, 2), total_dollars=round(e_total, 2),
            is_engine_point=1,
        ))
        print(f"  engine: days={e_days:+.3f}  spoilage=${e_spoil:,.0f}  missed=${e_missed:,.0f}  "
              f"carrying=${e_carry:,.0f}  total=${e_total:,.0f}")

        swept = (pd.DataFrame([r for r in frontier_rows if r["family"] == family and not r["is_engine_point"]])
                   .sort_values("z").reset_index(drop=True))
        analyses.append(empirical_critical_ratio(swept, family, cur))

    # scratch scenario cleanup -- sweep par rows were a means, not an artifact
    cur.execute("DELETE FROM dbo.par_levels WHERE scenario_key = ?", SCRATCH_SCENARIO_KEY)
    conn.commit()

    print("\n=== empirical vs modeled critical ratio (the reportable model error) ===")
    for a in analyses:
        loc = "interior" if a["interior"] else "BOUNDARY (interval still wrong)"
        print(f"{a['family']}: empirical z*={a['z_star']:+.2f} ({loc})")
        print(f"  critical ratio: modeled {a['ratio_modeled']:.3f} -> empirical {a['ratio_empirical']:.3f}")
        print(f"  overstock cost Co (Cu=${a['cu_avg']:.2f}/unit): modeled ${a['co_modeled']:.2f} -> "
              f"implied ${a['co_implied']:.2f}  ({a['co_implied']/a['co_modeled']:.1f}x the model's estimate)")

    frontier = pd.DataFrame(frontier_rows)
    frontier["z"] = frontier["z"].astype(object).where(frontier["z"].notna(), None)

    with open(DDL_FILE) as f:
        for batch in f.read().split("GO"):
            batch = batch.strip()
            if batch:
                cur.execute(batch)
    conn.commit()

    cur.executemany(
        """
        INSERT INTO dbo.frontier_dollar
            (family, z, days_of_safety_stock, spoilage_dollars, missed_sales_dollars,
             carrying_dollars, total_dollars, is_engine_point)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        frontier[["family", "z", "days_of_safety_stock", "spoilage_dollars",
                   "missed_sales_dollars", "carrying_dollars", "total_dollars", "is_engine_point"]].values.tolist(),
    )
    conn.commit()

    # ---------------------------------------------------------------- gate
    row_count = cur.execute("SELECT COUNT(*) FROM dbo.frontier_dollar").fetchone()[0]
    expected = sum(len(g) + 1 for g in FAMILY_Z_GRIDS.values())
    print("\n--- validation gate ---")
    print(f"{'PASS' if row_count == expected else 'FAIL'}: frontier_dollar rows {row_count} == expected {expected}")
    for a in analyses:
        print(f"{'PASS' if a['interior'] else 'FAIL'}: {a['family']} total-cost minimum is INTERIOR "
              f"(z*={a['z_star']:+.2f})")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
