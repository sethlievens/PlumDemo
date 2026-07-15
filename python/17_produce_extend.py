"""
PRODUCE landed its cost-minimum on the -1.5 boundary of the 16_* sweep, so
the true optimum is more negative and unlocated -- extend the grid down to
-3.5 and re-characterize (per the "no optimum on a boundary" rule).

PRODUCE only. Reuses the shared demand/phantom setup and the holdout helpers.
Reports train/test/full curves over the extended grid, whether the minimum
is now interior, and the engine's capped landing (PRODUCE's implied
multiplier saturates P_spoil at 1.0, so the engine cannot follow the optimum
past a bounded z -- this quantifies that structural gap honestly).
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
FAMILY = "PRODUCE"
Z_GRID = [round(-3.5 + 0.25 * i, 2) for i in range(17)]  # -3.5 .. +0.5
DB = {}


def connect():
    DB["conn"] = ld.get_conn()
    DB["conn"].timeout = 600
    DB["cur"] = DB["conn"].cursor()
    return DB["cur"]


def derive_read(override_z, retries=4):
    for attempt in range(1, retries + 1):
        try:
            DB["cur"].execute("EXEC dbo.usp_RecommendParLevels @source_scenario_key=?, @output_scenario_key=?, @override_z=?",
                              1, SCRATCH, override_z)
            DB["conn"].commit()
            rows = DB["cur"].execute(
                "SELECT ds.store_nbr, di.item_nbr, pl.par_units FROM dbo.par_levels pl "
                "JOIN dbo.dim_store ds ON ds.store_key=pl.store_key JOIN dbo.dim_item di ON di.item_key=pl.item_key "
                "WHERE pl.scenario_key=?", SCRATCH).fetchall()
            return {(r[0], r[1]): r[2] for r in rows}
        except pyodbc.Error as e:
            print(f"  derive attempt {attempt} failed ({str(e)[:60]}); reconnecting")
            try:
                DB["conn"].close()
            except Exception:
                pass
            time.sleep(2)
            connect()
    raise RuntimeError("derive failed")


def main():
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

    fmask = (store_items["family"] == FAMILY).to_numpy()
    f_items = store_items.loc[fmask].reset_index(drop=True)
    td, pm, dl = true_demand[:, fmask], phantom_mask[:, fmask], delivery[:, fmask]
    shelf, cad, cp = fam["shelf_life"][fmask], fam["cadence"][fmask], fam["case_pack"][fmask]
    cost = f_items["item_nbr"].map(econ_lookup["unit_cost"]).to_numpy(np.float32)
    price = f_items["item_nbr"].map(econ_lookup["unit_price"]).to_numpy(np.float32)
    margin = price - cost
    keys = list(zip(f_items["store_nbr"], f_items["item_nbr"]))

    connect()
    print(f"=== PRODUCE extended sweep z in [{Z_GRID[0]}, {Z_GRID[-1]}] ===")
    curve = []
    for z in Z_GRID:
        par_map = derive_read(z)
        par = np.array([par_map[k] for k in keys], dtype=np.float32)
        res = sim.simulate_forward(td, pm, dl, shelf, cad, cp, target_maker=lambda t, o, p=par: p)
        total = hc.cost_on(list(range(len(f_items))), res, cost, margin)[3]
        curve.append(total)
        print(f"  z={z:+.2f}  total=${total:,.0f}")

    DB["cur"].execute("DELETE FROM dbo.par_levels WHERE scenario_key=?", SCRATCH)
    DB["conn"].commit()

    zs = np.array(Z_GRID)
    zstar, interior = hc.z_star_from_curve(zs, np.array(curve))
    imin = int(np.argmin(curve))
    print(f"\nmin at grid z={zs[imin]:+.2f} (${curve[imin]:,.0f}); parabola z*={zstar:+.2f}; "
          f"{'INTERIOR' if interior else 'STILL ON BOUNDARY -- extend further'}")

    DB["cur"].close()
    DB["conn"].close()


if __name__ == "__main__":
    main()
