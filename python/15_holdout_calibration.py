"""
Holdout validation of the v2 per-family spoilage multiplier -- guards against
the circularity of fitting the multiplier on the sweep and then evaluating it
against that same sweep's minimum.

Protocol (per family, DELI & DAIRY):
  1. Split stores: TRAIN = store_nbr {3,13,28} (store_key 1-3),
                   TEST  = store_nbr {34,41,44} (store_key 4-6).
  2. Sweep z once over the full family slice; aggregate the dollar cost
     SEPARATELY for train stores and test stores (one sim per z point yields
     both curves -- the train/test store-items had independent demand draws).
  3. Fit multiplier_train from the TRAIN curve's minimum (implied Co).
     Fit multiplier_test from the TEST curve's minimum, independently, ONLY
     to check stability -- never used to build the engine.
  4. Set dbo.dim_spoilage_calibration = multiplier_train, derive the family's
     pars into a scratch scenario, and simulate them on the TEST stores.
     Compare the engine's TEST total cost to the TEST curve's own minimum
     (the held-out optimum). This is the honest, non-circular landing.
  5. Report multiplier_train vs multiplier_test: if DELI ~2.8x and DAIRY
     ~4.0x hold on both halves, the multiplier is estimating a real physical
     quantity (safety stock's tail spoilage rate / average spoilage rate),
     not a fudge factor.

After validation, dbo.dim_spoilage_calibration is restored to the FULL-DATA
fit (all 6 stores) -- standard practice: validate on holdout, deploy on all
data -- and scenarios 3/4/5 are re-derived from it so production is unchanged.
"""

import importlib.util
import math

import numpy as np
import pandas as pd


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

bf = _load("backfill", "python/05_backfill.py")
ld = _load("load", "python/08_load.py")
sim = _load("simulate", "python/12_simulate.py")

SCRATCH_SCENARIO_KEY = 7
SOURCE_SCENARIO_KEY = 1
TRAIN_NBRS = {3, 13, 28}   # store_key 1-3
TEST_NBRS = {34, 41, 44}   # store_key 4-6
FAMILY_Z_GRIDS = {
    "DELI":  [round(-1.5 + 0.25 * i, 2) for i in range(11)],
    "DAIRY": [round(-0.5 + 0.25 * i, 2) for i in range(11)],
}


def norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def z_star_from_curve(zs, totals):
    """Parabola-vertex z* around the discrete minimum; boundary z if the min
    is at an edge (no interior vertex to fit)."""
    i = int(np.argmin(totals))
    if 0 < i < len(zs) - 1:
        a, b, _c = np.polyfit(zs[i - 1:i + 2], totals[i - 1:i + 2], 2)
        if a > 0:
            return float(-b / (2 * a)), True
    return float(zs[i]), (0 < i < len(zs) - 1)


def multiplier_from_zstar(z_star, cu, unit_cost, S, W, carrying_rate=0.25):
    """Back out the per-family spoilage multiplier the empirical optimum
    implies: k = (Co_implied - carrying) / (exp(-S/W) * unit_cost)."""
    ratio = norm_cdf(z_star)
    co_implied = cu * (1.0 - ratio) / ratio if ratio > 0 else float("inf")
    carrying = unit_cost * carrying_rate * (W / 365.0)
    exp_term = math.exp(-S / W) * unit_cost
    return (co_implied - carrying) / exp_term, ratio, co_implied


def cost_on(cols, res, cost, margin):
    spoil = float((res["waste"][:, cols] * cost[cols][None, :]).sum())
    missed = float((res["lost"][:, cols] * margin[cols][None, :]).sum())
    carrying = float((res["closing"][:, cols] * cost[cols][None, :]).sum()
                     * sim.CARRYING_COST_ANNUAL_RATE / 365)
    return spoil, missed, carrying, spoil + missed + carrying


def main():
    print("=== setup: shared demand/phantom generation ===")
    grid, econ = bf.load_inputs()
    mats = bf.build_matrices(grid, econ)
    store_items = mats["store_items"]
    fam = bf.family_arrays(store_items, bf.CALIBRATED_BUFFER)
    dow_family_table = bf.dow_index_table(grid, econ, fam["fam_list"])

    mean_units, mean_cv, nb_r = sim.fit_nb_dispersion(grid, econ)
    fam["nb_r"] = np.array([nb_r.to_dict()[f] for f in store_items["family"]], dtype=np.float32)

    conn = ld.get_conn()
    cur = conn.cursor()
    vel_seed = sim.sql_last_28_days_sales(cur, store_items)
    clean_velocity = sim.sql_clean_velocity(cur, store_items)
    days_since = sim.sql_days_since_last_sale(cur, store_items)
    dow_idx = sim.sql_dow_index(cur, store_items)
    par_by_scenario = sim.sql_par_levels(cur, store_items)

    keep = ~np.isnan(par_by_scenario[3]) & ~np.isnan(clean_velocity) & (days_since <= 28)
    store_items = store_items.loc[keep].reset_index(drop=True)
    for k in fam:
        if isinstance(fam[k], np.ndarray):
            fam[k] = fam[k][keep]
    vel_seed, clean_velocity, dow_idx = vel_seed[:, keep], clean_velocity[keep], dow_idx[keep]

    dates = pd.date_range(sim.FORWARD_START, periods=sim.FORWARD_DAYS, freq="D")
    T, N = sim.FORWARD_DAYS, len(store_items)
    _, true_demand, _ = sim.calibrate_nb_dispersion(store_items, fam, clean_velocity, dow_idx, dates, mean_cv, nb_r)

    delivery = (np.arange(T)[:, None] % fam["cadence"][None, :].astype(np.int64)) == 0
    ref = sim.run_baseline(true_demand, np.zeros((T, N), bool), delivery, fam, vel_seed, dow_family_table, dates)
    events = sim.select_phantom_events(N, T, ref["closing"], np.random.default_rng(sim.SELECTION_SEED))
    phantom_mask = sim.build_phantom_mask(N, T, events)
    econ_lookup = econ.set_index("item_nbr")

    report = {}
    for family, z_grid in FAMILY_Z_GRIDS.items():
        print(f"\n=== {family}: split sweep (train {sorted(TRAIN_NBRS)} / test {sorted(TEST_NBRS)}) ===")
        fmask = (store_items["family"] == family).to_numpy()
        f_items = store_items.loc[fmask].reset_index(drop=True)
        td, pm, dl = true_demand[:, fmask], phantom_mask[:, fmask], delivery[:, fmask]
        shelf, cad, cp = fam["shelf_life"][fmask], fam["cadence"][fmask], fam["case_pack"][fmask]
        cost = f_items["item_nbr"].map(econ_lookup["unit_cost"]).to_numpy(np.float32)
        price = f_items["item_nbr"].map(econ_lookup["unit_price"]).to_numpy(np.float32)
        margin = price - cost
        keys = list(zip(f_items["store_nbr"], f_items["item_nbr"]))

        train_cols = [i for i, sn in enumerate(f_items["store_nbr"]) if sn in TRAIN_NBRS]
        test_cols = [i for i, sn in enumerate(f_items["store_nbr"]) if sn in TEST_NBRS]
        print(f"  train store-items: {len(train_cols)}, test store-items: {len(test_cols)}")

        train_curve, test_curve = [], []
        for z in z_grid:
            cur.execute("EXEC dbo.usp_RecommendParLevels @source_scenario_key=?, @output_scenario_key=?, @family_filter=?, @override_z=?",
                        SOURCE_SCENARIO_KEY, SCRATCH_SCENARIO_KEY, family, z)
            conn.commit()
            rows = cur.execute("SELECT ds.store_nbr, di.item_nbr, pl.par_units FROM dbo.par_levels pl "
                               "JOIN dbo.dim_store ds ON ds.store_key=pl.store_key JOIN dbo.dim_item di ON di.item_key=pl.item_key "
                               "WHERE pl.scenario_key=?", SCRATCH_SCENARIO_KEY).fetchall()
            par_map = {(r[0], r[1]): r[2] for r in rows}
            par = np.array([par_map[k] for k in keys], dtype=np.float32)
            res = sim.simulate_forward(td, pm, dl, shelf, cad, cp, target_maker=lambda t, o, p=par: p)
            train_curve.append(cost_on(train_cols, res, cost, margin)[3])
            test_curve.append(cost_on(test_cols, res, cost, margin)[3])

        zs = np.array(z_grid)
        tr, te = np.array(train_curve), np.array(test_curve)

        # family-avg economics per store group (item mix nearly identical, computed per group anyway)
        S, W = float(shelf[0]), float(2 * cad[0])
        cu_tr, uc_tr = float(margin[train_cols].mean()), float(cost[train_cols].mean())
        cu_te, uc_te = float(margin[test_cols].mean()), float(cost[test_cols].mean())

        zstar_tr, _ = z_star_from_curve(zs, tr)
        zstar_te, te_interior = z_star_from_curve(zs, te)
        k_train, _, _ = multiplier_from_zstar(zstar_tr, cu_tr, uc_tr, S, W)
        k_test, _, _ = multiplier_from_zstar(zstar_te, cu_te, uc_te, S, W)
        te_min_total = float(te.min())

        # --- held-out engine: fit multiplier on TRAIN, evaluate on TEST ---
        cur.execute("UPDATE dbo.dim_spoilage_calibration SET spoilage_multiplier=? WHERE family=?",
                    round(k_train, 3), family)
        conn.commit()
        cur.execute("EXEC dbo.usp_RecommendParLevels @source_scenario_key=?, @output_scenario_key=?, @family_filter=?, @override_z=?",
                    SOURCE_SCENARIO_KEY, SCRATCH_SCENARIO_KEY, family, None)
        conn.commit()
        rows = cur.execute("SELECT ds.store_nbr, di.item_nbr, pl.par_units FROM dbo.par_levels pl "
                           "JOIN dbo.dim_store ds ON ds.store_key=pl.store_key JOIN dbo.dim_item di ON di.item_key=pl.item_key "
                           "WHERE pl.scenario_key=?", SCRATCH_SCENARIO_KEY).fetchall()
        par_map = {(r[0], r[1]): r[2] for r in rows}
        eng_par = np.array([par_map[k] for k in keys], dtype=np.float32)
        eng_res = sim.simulate_forward(td, pm, dl, shelf, cad, cp, target_maker=lambda t, o, p=eng_par: p)
        eng_test_total = cost_on(test_cols, eng_res, cost, margin)[3]

        report[family] = dict(
            zstar_train=zstar_tr, zstar_test=zstar_te, te_interior=te_interior,
            k_train=k_train, k_test=k_test,
            eng_test_total=eng_test_total, te_min_total=te_min_total,
            gap_pct=100 * (eng_test_total - te_min_total) / te_min_total,
        )
        print(f"  z*_train={zstar_tr:+.3f}  z*_test={zstar_te:+.3f}")
        print(f"  multiplier: train={k_train:.2f}x  test={k_test:.2f}x")
        print(f"  HELD-OUT engine (train-fit) on test stores: ${eng_test_total:,.0f}  vs test optimum ${te_min_total:,.0f}  "
              f"(+{report[family]['gap_pct']:.1f}%)")

    # restore production calibration (full-data fit) + re-derive scenarios 3/4/5
    print("\n=== restore full-data production calibration + re-derive ===")
    cur.execute("DELETE FROM dbo.par_levels WHERE scenario_key=?", SCRATCH_SCENARIO_KEY)
    cur.execute("UPDATE dbo.dim_spoilage_calibration SET spoilage_multiplier=2.820 WHERE family='DELI'")
    cur.execute("UPDATE dbo.dim_spoilage_calibration SET spoilage_multiplier=4.180 WHERE family='DAIRY'")
    conn.commit()
    for sk, z in ((3, None), (4, 1.65), (5, 2.33)):
        cur.execute("EXEC dbo.usp_RecommendParLevels @source_scenario_key=1, @output_scenario_key=?, @override_z=?", sk, z)
    conn.commit()

    print("\n=== HOLDOUT VALIDATION SUMMARY ===")
    for family, r in report.items():
        stable = abs(r["k_train"] - r["k_test"]) / r["k_train"] < 0.30
        print(f"{family}:")
        print(f"  multiplier train {r['k_train']:.2f}x vs test {r['k_test']:.2f}x  "
              f"-> {'STABLE (real physical quantity)' if stable else 'MOVES (fudge-factor risk)'}")
        print(f"  held-out landing: engine ${r['eng_test_total']:,.0f} vs test optimum ${r['te_min_total']:,.0f} "
              f"(+{r['gap_pct']:.1f}%){'  [test optimum interior]' if r['te_interior'] else '  [test optimum at boundary]'}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
