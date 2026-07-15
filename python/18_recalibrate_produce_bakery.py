"""
Re-sweep + re-holdout PRODUCE and BREAD/BAKERY on the CORRECTED demand model
(after the dow_index fix in usp_CalculateVelocity, FINDINGS section 6). Their
§5b multipliers (PRODUCE 13.24x, BAKERY 2.50x) were fit on the old
broken-Friday demand and need re-validation.

Same protocol as python/16_holdout_calibration_all.py, no shortcuts:
  - WIDE z grid -3.5 .. +1.0 (PRODUCE's optimum is deep-negative; the -1.5
    floor used before landed it on a boundary). Boundary optima are flagged.
  - Fit multiplier on TRAIN stores (3/13/28), evaluate the corrected engine
    on held-out TEST stores (34/41/44) vs the test stores' own optimum.
  - One all-family derive per z serves both families; retry/reconnect on a
    dropped connection.

Production: writes the new full-data multipliers for these two families
(DELI/DAIRY/GROCERY/BEVERAGES untouched), then re-derives scenarios 3/4/5.
The sim re-run + gate check is done separately.
"""

import importlib.util
import time

import numpy as np
import pandas as pd
import pyodbc


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

bf = _load("backfill", "python/05_backfill.py")
ld = _load("load", "python/08_load.py")
sim = _load("simulate", "python/12_simulate.py")
hc = _load("holdout", "python/15_holdout_calibration.py")

SCRATCH = 7
TRAIN_NBRS, TEST_NBRS = hc.TRAIN_NBRS, hc.TEST_NBRS
FAMILIES = ["BREAD/BAKERY", "PRODUCE"]
Z_GRID = [round(-3.5 + 0.25 * i, 2) for i in range(19)]  # -3.5 .. +1.0
OLD = {"BREAD/BAKERY": 2.50, "PRODUCE": 13.24}
DB = {}


def connect():
    DB["conn"] = ld.get_conn()
    DB["conn"].timeout = 600
    DB["cur"] = DB["conn"].cursor()
    return DB["cur"]


def _retry(fn, what, retries=4):
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except pyodbc.Error as e:
            print(f"  {what}: attempt {attempt}/{retries} failed ({str(e)[:70]}); reconnecting")
            try:
                DB["conn"].close()
            except Exception:
                pass
            time.sleep(2)
            connect()
    raise RuntimeError(f"{what}: failed after {retries} attempts")


def derive_all_and_read(override_z):
    def once():
        DB["cur"].execute("EXEC dbo.usp_RecommendParLevels @source_scenario_key=?, @output_scenario_key=?, @override_z=?",
                          1, SCRATCH, override_z)
        DB["conn"].commit()
        rows = DB["cur"].execute(
            "SELECT ds.store_nbr, di.item_nbr, pl.par_units FROM dbo.par_levels pl "
            "JOIN dbo.dim_store ds ON ds.store_key=pl.store_key JOIN dbo.dim_item di ON di.item_key=pl.item_key "
            "WHERE pl.scenario_key=?", SCRATCH).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}
    return _retry(once, "derive")


def set_multiplier(family, value, note):
    def once():
        DB["cur"].execute("DELETE FROM dbo.dim_spoilage_calibration WHERE family=?", family)
        DB["cur"].execute("INSERT INTO dbo.dim_spoilage_calibration (family, spoilage_multiplier, source_note) VALUES (?,?,?)",
                          family, round(value, 3), note)
        DB["conn"].commit()
    _retry(once, f"set_multiplier {family}")


def par_vector(par_map, keys):
    v = np.array([par_map.get(k, np.nan) for k in keys], dtype=np.float32)
    assert not np.isnan(v).any(), f"{int(np.isnan(v).sum())} store-items missing from derive"
    return v


def main():
    print("=== setup: shared demand/phantom generation (corrected dow_index) ===")
    grid, econ = bf.load_inputs()
    mats = bf.build_matrices(grid, econ)
    store_items = mats["store_items"]
    fam = bf.family_arrays(store_items, bf.CALIBRATED_BUFFER)
    dow_family_table = bf.dow_index_table(grid, econ, fam["fam_list"])
    mean_units, mean_cv, nb_r = sim.fit_nb_dispersion(grid, econ)
    fam["nb_r"] = np.array([nb_r.to_dict()[f] for f in store_items["family"]], dtype=np.float32)

    cur = connect()
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

    D = {}
    for family in FAMILIES:
        fmask = (store_items["family"] == family).to_numpy()
        f_items = store_items.loc[fmask].reset_index(drop=True)
        cost = f_items["item_nbr"].map(econ_lookup["unit_cost"]).to_numpy(np.float32)
        price = f_items["item_nbr"].map(econ_lookup["unit_price"]).to_numpy(np.float32)
        D[family] = dict(
            f_items=f_items, td=true_demand[:, fmask], pm=phantom_mask[:, fmask], dl=delivery[:, fmask],
            shelf=fam["shelf_life"][fmask], cad=fam["cadence"][fmask], cp=fam["case_pack"][fmask],
            cost=cost, margin=price - cost,
            keys=list(zip(f_items["store_nbr"], f_items["item_nbr"])),
            train_cols=[i for i, sn in enumerate(f_items["store_nbr"]) if sn in TRAIN_NBRS],
            test_cols=[i for i, sn in enumerate(f_items["store_nbr"]) if sn in TEST_NBRS],
            all_cols=list(range(len(f_items))),
            curves={"train": [], "test": [], "full": []},
        )

    connect()  # fresh connection for the derive loop
    print(f"\n=== sweep z in [{Z_GRID[0]}, {Z_GRID[-1]}] ({len(Z_GRID)} points), PRODUCE + BREAD/BAKERY ===")
    for z in Z_GRID:
        par_map = derive_all_and_read(z)
        for family in FAMILIES:
            d = D[family]
            par = par_vector(par_map, d["keys"])
            res = sim.simulate_forward(d["td"], d["pm"], d["dl"], d["shelf"], d["cad"], d["cp"],
                                       target_maker=lambda t, o, p=par: p)
            d["curves"]["train"].append(hc.cost_on(d["train_cols"], res, d["cost"], d["margin"])[3])
            d["curves"]["test"].append(hc.cost_on(d["test_cols"], res, d["cost"], d["margin"])[3])
            d["curves"]["full"].append(hc.cost_on(d["all_cols"], res, d["cost"], d["margin"])[3])
        print(f"  z={z:+.2f} done")

    zs = np.array(Z_GRID)
    report = {}
    for family in FAMILIES:
        d = D[family]
        tr, te, fu = (np.array(d["curves"][k]) for k in ("train", "test", "full"))
        S, W = float(d["shelf"][0]), float(2 * d["cad"][0])
        zstar_tr, tr_int = hc.z_star_from_curve(zs, tr)
        zstar_te, te_int = hc.z_star_from_curve(zs, te)
        zstar_fu, fu_int = hc.z_star_from_curve(zs, fu)
        cu_tr, uc_tr = float(d["margin"][d["train_cols"]].mean()), float(d["cost"][d["train_cols"]].mean())
        cu_te, uc_te = float(d["margin"][d["test_cols"]].mean()), float(d["cost"][d["test_cols"]].mean())
        cu_fu, uc_fu = float(d["margin"].mean()), float(d["cost"].mean())
        report[family] = dict(
            zstar_train=zstar_tr, zstar_test=zstar_te, zstar_full=zstar_fu,
            boundary=not (tr_int and te_int and fu_int),
            k_train=hc.multiplier_from_zstar(zstar_tr, cu_tr, uc_tr, S, W)[0],
            k_test=hc.multiplier_from_zstar(zstar_te, cu_te, uc_te, S, W)[0],
            k_full=hc.multiplier_from_zstar(zstar_fu, cu_fu, uc_fu, S, W)[0],
            te_min_total=float(te.min()))

    print("\n=== held-out engine: train-fit multipliers, evaluate on test ===")
    for family in FAMILIES:
        set_multiplier(family, report[family]["k_train"], "HELDOUT train-fit (temp)")
    par_map = derive_all_and_read(None)
    for family in FAMILIES:
        d = D[family]
        eng_par = par_vector(par_map, d["keys"])
        res = sim.simulate_forward(d["td"], d["pm"], d["dl"], d["shelf"], d["cad"], d["cp"],
                                   target_maker=lambda t, o, p=eng_par: p)
        eng_test = hc.cost_on(d["test_cols"], res, d["cost"], d["margin"])[3]
        report[family]["eng_test_total"] = eng_test
        report[family]["gap_pct"] = 100 * (eng_test - report[family]["te_min_total"]) / report[family]["te_min_total"]

    print("=== write production calibration + re-derive scenarios 3/4/5 ===")
    DB["cur"].execute("DELETE FROM dbo.par_levels WHERE scenario_key=?", SCRATCH)
    DB["conn"].commit()
    for family, r in report.items():
        set_multiplier(family, r["k_full"],
                       f"Recalibrated on corrected demand; holdout train {r['k_train']:.2f}x/test {r['k_test']:.2f}x")
    for sk, z in ((3, None), (4, 1.65), (5, 2.33)):
        DB["cur"].execute("EXEC dbo.usp_RecommendParLevels @source_scenario_key=?, @output_scenario_key=?, @override_z=?", 1, sk, z)
    DB["conn"].commit()

    print("\n=== RECALIBRATION SUMMARY ===")
    for family, r in report.items():
        stable = abs(r["k_train"] - r["k_test"]) / r["k_train"] < 0.30
        print(f"{family}: multiplier OLD {OLD[family]:.2f}x -> NEW {r['k_full']:.2f}x "
              f"(train {r['k_train']:.2f}x / test {r['k_test']:.2f}x -> {'STABLE' if stable else 'MOVES'}); "
              f"held-out ${r['eng_test_total']:,.0f} vs test optimum ${r['te_min_total']:,.0f} (+{r['gap_pct']:.1f}%)"
              f"{'  [BOUNDARY - extend]' if r['boundary'] else ''}")

    DB["cur"].close()
    DB["conn"].close()


if __name__ == "__main__":
    main()
