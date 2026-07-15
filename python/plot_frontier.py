"""
Renders artifacts/frontier.parquet into artifacts/frontier_chart.png: one
small-multiple panel per family, buffer on x, waste%/top-up% sharing a single
percent y-axis (never dual-axis -- both are rates, so one scale is honest).
This is a static preview of what becomes a Power BI page; not itself wired
to Power BI.
"""

import matplotlib.pyplot as plt
import pandas as pd

BLUE = "#2a78d6"
AQUA = "#1baf7a"
RED = "#d03b3b"
GRID = "#e1e0d9"
MUTED = "#898781"
INK = "#0b0b0b"
BAND = "#cde2fb"

WASTE_ANCHOR = {
    "PRODUCE": (4, 8),
    "BREAD/BAKERY": (8, 14),
    "DELI": (10, 15),
    "DAIRY": (1, 3),
    "GROCERY": (0, 1),
}
FROZEN_BUFFER = {
    "PRODUCE": 1.65, "DAIRY": 2.58, "DELI": 1.65,
    "BREAD/BAKERY": 1.65, "BEVERAGES": 1.65, "GROCERY": 2.06,
}
STATUS = {
    "PRODUCE": "near-miss (own top-up 7.7%)",
    "DAIRY": "converged",
    "DELI": "structural wall (top-up floor 7.5%)",
    "BREAD/BAKERY": "near-miss (own top-up 13.3% at frozen buffer)",
    "BEVERAGES": "no anchor",
    "GROCERY": "converged",
}
ORDER = ["PRODUCE", "DAIRY", "DELI", "BREAD/BAKERY", "BEVERAGES", "GROCERY"]


def main():
    df = pd.read_parquet("artifacts/frontier.parquet")

    fig, axes = plt.subplots(2, 3, figsize=(13, 7.5), sharex=True, sharey=True)
    fig.patch.set_facecolor("#fcfcfb")

    for ax, fam in zip(axes.flat, ORDER):
        sub = df[df["family"] == fam].sort_values("buffer")
        ax.set_facecolor("#fcfcfb")

        if fam in WASTE_ANCHOR:
            lo, hi = WASTE_ANCHOR[fam]
            ax.axhspan(lo, hi, color=BAND, alpha=0.6, zorder=0, label="_nolegend_")

        ax.axhline(5, color=RED, linestyle=(0, (4, 3)), linewidth=1.2, zorder=1)
        ax.axvline(FROZEN_BUFFER[fam], color=MUTED, linestyle=(0, (2, 2)), linewidth=1.2, zorder=1)

        ax.plot(sub["buffer"], sub["waste_pct"], color=BLUE, linewidth=2.2, marker="o", markersize=4, zorder=3)
        ax.plot(sub["buffer"], sub["emergency_rate"], color=AQUA, linewidth=2.2, marker="o", markersize=4, zorder=3)

        ax.set_title(fam, fontsize=11, fontweight="bold", color=INK, loc="left")
        ax.text(0.02, 0.96, STATUS[fam], transform=ax.transAxes, fontsize=8.5,
                color=MUTED, va="top", ha="left", style="italic")

        ax.grid(True, color=GRID, linewidth=0.8, zorder=0)
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["left", "bottom"]].set_color(MUTED)
        ax.tick_params(colors=MUTED, labelsize=9)

    for ax in axes[-1]:
        ax.set_xlabel("buffer multiplier", fontsize=9, color=MUTED)
    for ax in axes[:, 0]:
        ax.set_ylabel("percent", fontsize=9, color=MUTED)

    handles = [
        plt.Line2D([0], [0], color=BLUE, linewidth=2.2, marker="o", markersize=4, label="waste %"),
        plt.Line2D([0], [0], color=AQUA, linewidth=2.2, marker="o", markersize=4, label="emergency top-up %"),
        plt.Line2D([0], [0], color=RED, linestyle=(0, (4, 3)), linewidth=1.2, label="5% top-up gate"),
        plt.Line2D([0], [0], color=MUTED, linestyle=(0, (2, 2)), linewidth=1.2, label="frozen buffer"),
        plt.Rectangle((0, 0), 1, 1, color=BAND, alpha=0.6, label="waste anchor band"),
    ]
    fig.legend(handles=handles, loc="upper center", ncol=5, frameon=False,
               fontsize=9, labelcolor=INK, bbox_to_anchor=(0.5, 1.02))

    fig.suptitle("Buffer vs. waste% vs. top-up% -- the calibration frontier, per family",
                 fontsize=13, fontweight="bold", color=INK, y=1.08)

    fig.tight_layout(rect=[0, 0, 1, 1.0])
    fig.savefig("artifacts/frontier_chart.png", dpi=200, bbox_inches="tight", facecolor=fig.get_facecolor())
    print("saved artifacts/frontier_chart.png")


if __name__ == "__main__":
    main()
