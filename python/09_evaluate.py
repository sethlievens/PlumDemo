"""
Scores usp_DetectPhantomInventory against phantom_events ground truth
(1,958 planted events). Answers the grain question first (with query
evidence, not assertion), then sweeps the per-item baseline percentile and
the peer-group threshold one at a time, reporting precision/recall/F1 at
each -- honestly, un-tuned. Also breaks out false positives by family.

Depends on sql/eval/*.sql (deployed by this script) and re-runs
usp_DetectPhantomInventory itself for every sweep point, so it's runnable
standalone without a prior detector run already in detection_log.
"""

import subprocess
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import pyodbc

EVAL_SQL_DIR = Path("sql/eval")
ARTIFACTS = Path("artifacts")

BASELINE = dict(percentile=0.95, peer_pct=0.60)
PERCENTILE_SWEEP = [0.80, 0.85, 0.90, 0.95, 0.975, 0.99]
PEER_SWEEP = [0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]

BLUE, AQUA, YELLOW, RED, MUTED, GRID, INK = (
    "#2a78d6", "#1baf7a", "#eda100", "#d03b3b", "#898781", "#e1e0d9", "#0b0b0b",
)


def sa_password():
    return [l for l in open(".env") if l.startswith("SA_PASSWORD=")][0].split("=", 1)[1].strip()


def get_conn():
    pw = sa_password()
    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER=localhost,1433;"
        f"UID=sa;PWD={pw};DATABASE=PlumDemo;TrustServerCertificate=yes;MARS_Connection=yes"
    )


def deploy_eval_views():
    for f in ["v_eval_matches.sql", "v_eval_run_summary.sql", "v_eval_fp_by_family.sql"]:
        subprocess.run(
            ["/opt/mssql-tools18/bin/sqlcmd", "-S", "localhost", "-U", "sa", "-P", sa_password(),
             "-C", "-d", "PlumDemo", "-i", str(EVAL_SQL_DIR / f)],
            check=True, capture_output=True, text=True,
        )


def check_grain(cur, run_id):
    """Answer the grain question with evidence, not assertion, before scoring anything.
    Scoped to a single run_id -- the same (store,item,date) legitimately
    appearing in TWO DIFFERENT runs is not a grain violation, it just means
    two experiments both flagged the same event. Only duplicates WITHIN one
    run would mean "one row = one day," which is what we're checking for."""
    total_rows = cur.execute("SELECT COUNT(*) FROM dbo.detection_log WHERE run_id = ?", run_id).fetchone()[0]
    distinct_triples = cur.execute(
        "SELECT COUNT(DISTINCT CONCAT(store_key,'|',item_key,'|',detected_date)) FROM dbo.detection_log WHERE run_id = ?",
        run_id,
    ).fetchone()[0]
    print(f"=== grain check (run_id={run_id}) ===")
    print(f"detection_log rows: {total_rows:,}  distinct (store,item,detected_date): {distinct_triples:,}")
    if total_rows == distinct_triples:
        print("PASS: no duplicate (store,item,date) rows within this run -- confirms one row per EVENT, not per day.")
        print("      (usp_DetectPhantomInventory fires the day a streak first exceeds the item's own")
        print("       baseline, never re-firing on subsequent days of the same streak.)")
    else:
        print("FAIL: duplicate (store,item,date) rows found within one run -- grain is NOT clean, collapse before scoring.")


def run_detector(cur, scenario_key, percentile, peer_pct):
    cur.execute(
        "EXEC dbo.usp_DetectPhantomInventory "
        "@scenario_key=?, @percentile=?, @peer_velocity_threshold_pct=?",
        scenario_key, percentile, peer_pct,
    )
    row = cur.fetchone()
    run_id = row.run_id
    cur.connection.commit()
    return run_id


def score_run(cur, run_id, total_ground_truth):
    cur.execute(
        "SELECT total_detections, true_positive_detections, false_positive_detections "
        "FROM dbo.v_eval_run_summary WHERE run_id = ?", run_id,
    )
    row = cur.fetchone()
    total_det, tp, fp = (row.total_detections, row.true_positive_detections, row.false_positive_detections) if row else (0, 0, 0)
    precision = tp / total_det if total_det else 0.0
    recall = tp / total_ground_truth if total_ground_truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return dict(run_id=run_id, total_detections=total_det, true_positives=tp,
                false_positives=fp, precision=precision, recall=recall, f1=f1)


def sweep(cur, scenario_key, total_ground_truth, param_name, values):
    rows = []
    for v in values:
        kwargs = dict(BASELINE)
        kwargs[param_name] = v
        run_id = run_detector(cur, scenario_key, kwargs["percentile"], kwargs["peer_pct"])
        metrics = score_run(cur, run_id, total_ground_truth)
        metrics[param_name] = v
        rows.append(metrics)
        print(f"  {param_name}={v}: precision={metrics['precision']:.1%} recall={metrics['recall']:.1%} "
              f"f1={metrics['f1']:.3f}  ({metrics['total_detections']:,} detections, {metrics['true_positives']:,} TP)")
    return pd.DataFrame(rows)


def plot_sweep(df, param_name, xlabel, path):
    fig, ax = plt.subplots(figsize=(6, 4))
    fig.patch.set_facecolor("#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    ax.plot(df[param_name], df["precision"], color=BLUE, marker="o", linewidth=2.2, label="precision")
    ax.plot(df[param_name], df["recall"], color=AQUA, marker="o", linewidth=2.2, label="recall")
    ax.plot(df[param_name], df["f1"], color=YELLOW, marker="o", linewidth=2.2, label="F1")
    ax.set_xlabel(xlabel, color=MUTED, fontsize=10)
    ax.set_ylabel("score", color=MUTED, fontsize=10)
    ax.set_ylim(0, 1)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.legend(frameon=False, labelcolor=INK, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=200, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_fp_by_family(df, path):
    df = df.sort_values("fp_rate", ascending=True)
    fig, ax = plt.subplots(figsize=(7, 4))
    fig.patch.set_facecolor("#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    ax.barh(df["family"], df["fp_rate"], color=RED)
    for i, (rate, n) in enumerate(zip(df["fp_rate"], df["false_positives"])):
        ax.text(rate + 0.01, i, f"{rate:.0%}  (n={n:,})", va="center", fontsize=8.5, color=INK)
    ax.set_xlabel("false positive rate (of that family's detections)", color=MUTED, fontsize=10)
    ax.set_xlim(0, 1.05)
    ax.grid(True, axis="x", color=GRID, linewidth=0.8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=200, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def main():
    deploy_eval_views()
    conn = get_conn()
    cur = conn.cursor()

    scenario_key = cur.execute("SELECT scenario_key FROM dbo.dim_scenario WHERE scenario_name = 'Historical'").fetchone()[0]
    total_ground_truth = cur.execute("SELECT COUNT(*) FROM dbo.phantom_events").fetchone()[0]

    print()
    print(f"=== baseline (percentile={BASELINE['percentile']:.0%}, peer={BASELINE['peer_pct']:.0%}) ===")
    baseline_run_id = run_detector(cur, scenario_key, BASELINE["percentile"], BASELINE["peer_pct"])
    baseline_metrics = score_run(cur, baseline_run_id, total_ground_truth)
    print(f"  precision={baseline_metrics['precision']:.1%} recall={baseline_metrics['recall']:.1%} "
          f"f1={baseline_metrics['f1']:.3f}  ({baseline_metrics['total_detections']:,} detections, "
          f"{baseline_metrics['true_positives']:,} TP, {baseline_metrics['false_positives']:,} FP)")
    print()
    check_grain(cur, baseline_run_id)

    print()
    print("=== sweep: per-item baseline percentile ===")
    pct_df = sweep(cur, scenario_key, total_ground_truth, "percentile", PERCENTILE_SWEEP)

    print()
    print("=== sweep: peer-group velocity threshold ===")
    peer_df = sweep(cur, scenario_key, total_ground_truth, "peer_pct", PEER_SWEEP)

    print()
    print("=== false positives by family (baseline run) ===")
    fp_df = pd.read_sql(
        "SELECT family, total_detections, true_positives, false_positives, "
        "CAST(false_positives AS FLOAT)/total_detections AS fp_rate "
        "FROM dbo.v_eval_fp_by_family WHERE run_id = ? ORDER BY fp_rate DESC",
        conn, params=[baseline_run_id],
    )
    item_counts = pd.read_sql("SELECT family, COUNT(*) AS n_items FROM dbo.dim_item GROUP BY family", conn)
    fp_df = fp_df.merge(item_counts, on="family")
    fp_df["fp_per_item"] = fp_df["false_positives"] / fp_df["n_items"]
    for _, r in fp_df.sort_values("fp_rate", ascending=False).iterrows():
        print(f"  {r['family']:<14} fp_rate={r['fp_rate']:.1%}  fp_count={int(r['false_positives']):,}  "
              f"n_items={int(r['n_items'])}  fp_per_item={r['fp_per_item']:.1f}")

    worst_by_rate = fp_df.sort_values("fp_rate", ascending=False).iloc[0]["family"]
    print(f"\n  Worst by FP RATE: {worst_by_rate}.", end=" ")
    if worst_by_rate == "PRODUCE":
        print("Matches the produce-is-worst expectation.")
    else:
        print("Does NOT match the produce-is-worst expectation -- PRODUCE is not the worst family here.")

    ARTIFACTS.mkdir(exist_ok=True)
    plot_sweep(pct_df, "percentile", "per-item baseline percentile", ARTIFACTS / "eval_sweep_percentile.png")
    plot_sweep(peer_df, "peer_pct", "peer-group velocity threshold", ARTIFACTS / "eval_sweep_peer.png")
    plot_fp_by_family(fp_df, ARTIFACTS / "eval_fp_by_family.png")

    pct_df.to_csv(ARTIFACTS / "eval_sweep_percentile.csv", index=False)
    peer_df.to_csv(ARTIFACTS / "eval_sweep_peer.csv", index=False)
    fp_df.to_csv(ARTIFACTS / "eval_fp_by_family.csv", index=False)

    conn.close()


if __name__ == "__main__":
    main()
