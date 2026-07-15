"""
Calibrate 05_backfill.py's per-family buffer against the CLAUDE.md industry
anchors: one proportional-control knob per family, targeting waste% (the
anchor given for every family except BEVERAGES, which has none and is left
at the 05_backfill.py default, untouched).

Per docs/BACKFILL.md, waste% and days-of-supply move TOGETHER with buffer
(more buffer -> more stock sitting around -> both rise), while the emergency
top-up rate moves the OPPOSITE way (more buffer -> fewer stockouts). For a
family whose shelf life is very tight relative to its demand variability
(short shelf life, high forecast-error sigma), there may be no buffer value
that lands waste% inside its anchor band AND keeps top-ups under the 5%
gate. That is reported as a finding, not silently resolved -- if fixing
waste requires lowering the buffer while top-ups are already over 5%,
lowering it further only makes the top-up problem worse, so the controller
freezes rather than "converging" on a buffer that quietly breaks the gate.

Run this only after 05_backfill.py's own structural gate (on_hand never
negative, accounting identity, emergency top-up < 5% under the DEFAULT
uniform buffer) has passed.
"""

import importlib.util

import numpy as np
import pandas as pd

spec = importlib.util.spec_from_file_location("backfill", "python/05_backfill.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)

STAGING = "data/staging"

WASTE_ANCHOR = {  # (low, high) percent, from CLAUDE.md
    "PRODUCE": (4, 8),
    "BREAD/BAKERY": (8, 14),
    "DELI": (10, 15),
    "DAIRY": (1, 3),
    "GROCERY": (0, 1),
}
DOS_ANCHOR = {  # (low, high) days, from CLAUDE.md -- informational check, not the controlled metric
    "PRODUCE": (1.5, 2.5),
    "DAIRY": (5, 8),
    "GROCERY": (14, 30),
}
EMERGENCY_GATE = 0.05
SWEEP_BUFFERS = [0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.6, 3.0, 4.0, 5.0]
GAIN = 0.6
MAX_STEP = 0.25  # cap on |fractional buffer change| per iteration, prevents single-step overshoot to the bounds
MAX_ITERS = 8
BUFFER_BOUNDS = (0.5, 4.0)


def sweep_frontier():
    rows = []
    for b in SWEEP_BUFFERS:
        _, by_family, _ = bf.run_backfill({fam: b for fam in bf.FAMILY_SIGMA})
        for fam, row in by_family.iterrows():
            rows.append(dict(
                family=fam, buffer=b,
                waste_pct=row["waste_pct"],
                days_of_supply=row["days_of_supply_weighted"],
                emergency_rate=100 * row["emergency_rate"],
            ))
    return pd.DataFrame(rows)


def frontier_feasible(frontier, fam):
    """Does ANY swept buffer for this family land waste in-band AND top-up < 5%?"""
    if fam not in WASTE_ANCHOR:
        return None
    lo, hi = WASTE_ANCHOR[fam]
    f = frontier[frontier["family"] == fam]
    hits = f[(f["waste_pct"] >= lo) & (f["waste_pct"] <= hi) & (f["emergency_rate"] < 100 * EMERGENCY_GATE)]
    return not hits.empty


def family_status(fam, by_family):
    """Error for every anchor this family has, normalized by the anchor's own
    BAND WIDTH (not its midpoint -- a family like GROCERY with a 0-1% waste
    anchor has a midpoint near zero, and dividing by that turns a trivial
    absolute miss into a ~100% relative error that blows up the controller).
    Positive error = needs a bigger buffer."""
    wlo, whi = WASTE_ANCHOR[fam]
    waste = by_family.loc[fam, "waste_pct"]
    waste_ok = wlo <= waste <= whi
    waste_err = ((wlo + whi) / 2 - waste) / (whi - wlo) if not waste_ok else 0.0

    dos_ok = True
    dos_err = 0.0
    if fam in DOS_ANCHOR:
        dlo, dhi = DOS_ANCHOR[fam]
        dos = by_family.loc[fam, "days_of_supply_weighted"]
        dos_ok = dlo <= dos <= dhi
        dos_err = ((dlo + dhi) / 2 - dos) / (dhi - dlo) if not dos_ok else 0.0

    return waste_ok, dos_ok, waste_err, dos_err


def calibrate():
    buffer = dict(bf.DEFAULT_BUFFER)
    frozen = {}  # fam -> reason string
    history = []
    final_it = 0

    for it in range(1, MAX_ITERS + 1):
        final_it = it
        _, by_family, _ = bf.run_backfill(buffer)
        all_ok = True

        for fam in WASTE_ANCHOR:
            emergency = by_family.loc[fam, "emergency_rate"]
            history.append(dict(iter=it, family=fam, buffer=buffer[fam],
                                 waste_pct=by_family.loc[fam, "waste_pct"],
                                 emergency_rate=100 * emergency,
                                 days_of_supply=by_family.loc[fam, "days_of_supply_weighted"]))

            waste_ok, dos_ok, waste_err, dos_err = family_status(fam, by_family)
            if waste_ok and dos_ok:
                continue
            all_ok = False
            if fam in frozen:
                continue

            need_more = (waste_err > 0) or (dos_err > 0)
            need_less = (waste_err < 0) or (dos_err < 0)

            if need_more and need_less:
                frozen[fam] = "waste-anchor and days-of-supply-anchor point in opposite buffer directions"
                continue
            if need_less and emergency >= EMERGENCY_GATE:
                frozen[fam] = "fixing waste/days-of-supply requires a smaller buffer, but top-up is already over the 5% gate -- smaller would only make that worse"
                continue

            active_errors = [e for e in (waste_err, dos_err) if e != 0.0]
            driver = sum(active_errors) / len(active_errors)
            step = float(np.clip(GAIN * driver, -MAX_STEP, MAX_STEP))
            buffer[fam] = float(np.clip(buffer[fam] * (1 + step), *BUFFER_BOUNDS))

        if all_ok or len(frozen) == len(WASTE_ANCHOR):
            break

    _, final_by_family, final_diag = bf.run_backfill(buffer)
    return buffer, final_by_family, final_diag, pd.DataFrame(history), final_it, frozen


def main():
    frontier = sweep_frontier()
    frontier.to_parquet(f"{STAGING}/calibration_frontier.parquet", index=False)

    buffer, by_family, diag, history, n_iters, frozen = calibrate()
    history.to_parquet(f"{STAGING}/calibration_history.parquet", index=False)
    by_family.to_parquet(f"{STAGING}/backfill_family_summary.parquet")

    print(f"=== calibration converged/stopped after {n_iters} iteration(s) ===")
    print()

    checks = [
        ("on_hand never negative", diag["on_hand_min"] >= -1e-3),
        ("accounting identity holds (max err < 0.01 units)", diag["max_abs_error"] < 1e-2),
        (f"blended emergency top-up rate < 5% (actual {diag['emergency_rate']:.1%})", diag["emergency_rate"] < EMERGENCY_GATE),
    ]

    for fam, (lo, hi) in WASTE_ANCHOR.items():
        waste = by_family.loc[fam, "waste_pct"]
        emergency = by_family.loc[fam, "emergency_rate"]
        checks.append((f"{fam} waste% in [{lo},{hi}] (actual {waste:.1f}%)", lo <= waste <= hi))
        checks.append((f"{fam} own emergency top-up < 5% (actual {emergency:.1%})", emergency < EMERGENCY_GATE))
        if fam in DOS_ANCHOR:
            dlo, dhi = DOS_ANCHOR[fam]
            dos = by_family.loc[fam, "days_of_supply_weighted"]
            checks.append((f"{fam} days-of-supply in [{dlo},{dhi}] (actual {dos:.1f})", dlo <= dos <= dhi))

    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}: {name}")

    if frozen:
        print()
        print("=== physically infeasible (frozen, not tuned around) ===")
        for fam in sorted(frozen):
            lo, hi = WASTE_ANCHOR[fam]
            waste = by_family.loc[fam, "waste_pct"]
            emergency = by_family.loc[fam, "emergency_rate"]
            feasible = frontier_feasible(frontier, fam)
            print(f"{fam}: waste {waste:.1f}% (anchor {lo}-{hi}%), top-up {emergency:.1%} (gate <5%), "
                  f"buffer frozen at {buffer[fam]:.2f}. Reason: {frozen[fam]}.")
            print(f"  10-step frontier sweep found a buffer clearing both waste-anchor and top-up-gate: "
                  f"{'yes -- recheck, this may be a coarse-grid miss' if feasible else 'no'}.")

    print()
    print("=== final buffers ===")
    for fam in sorted(bf.FAMILY_SIGMA):
        note = " (no anchor, left at default)" if fam not in WASTE_ANCHOR else ""
        print(f"  {fam}: {buffer[fam]:.2f}{note}")


if __name__ == "__main__":
    main()
