"""
Query tuning capture harness -- Round 2 indexing story.

For each of the three core procs (usp_CalculateVelocity, usp_DetectPhantomInventory,
usp_RecommendParLevels), captures:
  - duration and logical reads: read straight from sys.dm_exec_procedure_stats after
    a forced recompile (EXEC sp_recompile) + a single execution, so execution_count=1
    and total_logical_reads/total_worker_time/total_elapsed_time are exactly that one
    call's cost. NOT parsed from STATISTICS IO/TIME text -- pyodbc's cursor.messages
    is only reliably populated at unpredictable points relative to nextset() on this
    driver, and an earlier version of this script that parsed messages produced
    silently-corrupted numbers (messages re-observed across iterations, columnstore
    tables reporting "0 logical reads" because their I/O shows up as a separate
    "segment reads" line the regex didn't match at all). The DMV is also the more
    authoritative source a real DBA would reach for.
  - the actual execution plan, as a real SSMS-openable .sqlplan (merged from every
    statement in the proc: DELETE, the big WITH...INSERT, the final SELECT) and a
    custom-rendered PNG tree (this box is headless WSL2 -- no SSMS/ADS GUI to
    screenshot from, so the PNG is reconstructed directly from the ShowPlan XML's
    RelOp tree, not a literal screenshot; the .sqlplan is the authoritative artifact
    if a real SSMS screenshot is wanted later from the Windows side). Captured via
    SET STATISTICS XML ON in the SAME single post-recompile execution.
  - the dominant operator: the RelOp with the largest SELF cost (subtree cost minus
    children's subtree cost), which is what SSMS highlights with the thick arrow

Usage: python/17_capture_plan.py before|after
"""

import importlib.util
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

NS = {"sp": "http://schemas.microsoft.com/sqlserver/2004/07/showplan"}
ET.register_namespace("", NS["sp"])

SCRATCH_SCENARIO_KEY = 7  # 'DELI Sweep (scratch)' dim_scenario row -- reused so par_levels
                          # for the real Engine Recommended (3) / Conservative (4) /
                          # Aggressive (5) scenarios are never touched by this capture.


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ld = _load("load", "python/08_load.py")

PROCS = {
    "usp_CalculateVelocity": "EXEC dbo.usp_CalculateVelocity @scenario_key=1",
    "usp_DetectPhantomInventory": "EXEC dbo.usp_DetectPhantomInventory @scenario_key=1",
    "usp_RecommendParLevels": (
        f"EXEC dbo.usp_RecommendParLevels @source_scenario_key=1, "
        f"@output_scenario_key={SCRATCH_SCENARIO_KEY}"
    ),
}


def run_capture(cur, conn, proc_name, exec_sql):
    """Forces a fresh plan (sp_recompile evicts the cached plan + its
    dm_exec_procedure_stats row), runs exec_sql ONCE with STATISTICS XML ON,
    drains every statement's plan XML, then reads that single execution's
    authoritative cost straight from sys.dm_exec_procedure_stats.
    Returns (wall_ms, dmv_row, list_of_statement_xml)."""
    cur.execute(f"EXEC sp_recompile '{proc_name}'")
    conn.commit()

    cur.execute("SET STATISTICS XML ON")
    t0 = time.perf_counter()
    cur.execute(exec_sql)

    statement_xmls = []
    while True:
        desc = cur.description
        if desc and str(desc[0][0]).startswith("Microsoft SQL Server"):
            statement_xmls.append(cur.fetchval())
        elif desc:
            cur.fetchall()
        if not cur.nextset():
            break
    t1 = time.perf_counter()
    cur.execute("SET STATISTICS XML OFF")
    conn.commit()

    cur.execute("""
        SELECT execution_count, total_logical_reads, total_worker_time, total_elapsed_time
        FROM sys.dm_exec_procedure_stats
        WHERE object_id = OBJECT_ID(?)
    """, proc_name)
    row = cur.fetchone()
    assert row is not None and row[0] == 1, (
        f"expected exactly 1 execution recorded post-recompile for {proc_name}, "
        f"got {row} -- something else executed this proc concurrently"
    )
    dmv = dict(execution_count=row[0], total_logical_reads=row[1],
               total_worker_time_us=row[2], total_elapsed_time_us=row[3])

    return (t1 - t0) * 1000.0, dmv, statement_xmls


def parse_relops(xml_str, stmt_idx):
    """Flatten the RelOp tree into a list of dicts with self-cost computed
    (own EstimatedTotalSubtreeCost minus the sum of its direct children's)."""
    root = ET.fromstring(xml_str)
    nodes = []

    def walk(relop_el, depth, parent_id):
        node_id = len(nodes)
        phys = relop_el.get("PhysicalOp")
        logi = relop_el.get("LogicalOp")
        subtree_cost = float(relop_el.get("EstimatedTotalSubtreeCost", 0))
        est_rows = float(relop_el.get("EstimateRows", 0))
        exec_mode = relop_el.get("EstimatedExecutionMode", "Row")
        storage = None
        for child_tag in ("IndexScan", "IndexSeek", "ColumnStoreIndexScan"):
            el = relop_el.find(f"sp:{child_tag}", NS)
            if el is not None:
                storage = el.get("Storage")
                break
        rec = dict(stmt=stmt_idx, id=node_id, parent=parent_id, depth=depth,
                   physical_op=phys, logical_op=logi, subtree_cost=subtree_cost,
                   est_rows=est_rows, exec_mode=exec_mode, storage=storage,
                   self_cost=subtree_cost)
        nodes.append(rec)

        # RelOps live one level down inside operator-specific wrapper elements
        # (Hash, NestedLoops, ...) -- search all descendants, not just direct children,
        # but STOP at the first RelOp found on each branch (avoid double counting
        # a grandchild both directly and via its own parent).
        found = []
        for wrapper in list(relop_el):
            if wrapper.tag.endswith("}RelOp"):
                continue
            found.extend(wrapper.findall("sp:RelOp", NS))
        child_cost_sum = 0.0
        for child in found:
            child_cost_sum += float(child.get("EstimatedTotalSubtreeCost", 0))
            walk(child, depth + 1, node_id)
        rec["self_cost"] = max(0.0, subtree_cost - child_cost_sum)

    for stmt in root.findall(".//sp:StmtSimple", NS):
        top_relop = stmt.find("./sp:QueryPlan/sp:RelOp", NS)
        if top_relop is not None:
            walk(top_relop, 0, None)
    return nodes


def merge_sqlplan(statement_xmls):
    """Combine each statement's own <ShowPlanXML> doc into one, so the saved
    .sqlplan shows the whole proc (DELETE, the big INSERT, the final SELECT)
    as one openable file, exactly like SSMS's 'include actual plan' for a
    multi-statement batch would."""
    first = ET.fromstring(statement_xmls[0])
    statements_el = first.find(".//sp:Statements", NS)
    for xml_str in statement_xmls[1:]:
        other = ET.fromstring(xml_str)
        for stmt in other.findall(".//sp:StmtSimple", NS):
            statements_el.append(stmt)
    return ET.tostring(first, encoding="unicode")


def collapse_chains(nodes, cost_floor_pct=1.5):
    """SSMS shows every operator; a headless static PNG can't -- a query with
    several window functions stacks 40+ single-child Compute Scalar/Sequence
    Project nodes SQL Server uses to materialize each expression separately,
    which renders as an unreadable 10,000px-tall column. Collapse runs of
    low-cost (< cost_floor_pct of the statement) single-child nodes into the
    next structurally significant node (a branch point, a scan/seek, or
    anything over the cost floor), keeping a count + op-name footnote so the
    collapse is visible, not silently lossy. The full detail always remains
    in the paired .sqlplan."""
    total_cost = max((n["subtree_cost"] for n in nodes if n["parent"] is None), default=1.0) or 1.0
    by_id = {n["id"]: n for n in nodes}
    children_of = {}
    for n in nodes:
        children_of.setdefault(n["parent"], []).append(n["id"])

    def significant(n):
        pct = 100 * n["self_cost"] / total_cost
        return pct >= cost_floor_pct or len(children_of.get(n["id"], [])) != 1

    kept = []
    for n in nodes:
        if not significant(n):
            continue
        # walk up through collapsed (insignificant, single-child) ancestors
        # to find this node's nearest KEPT ancestor, and remember what got
        # skipped along the way for the footnote.
        collapsed_ops = []
        cur_parent = n["parent"]
        while cur_parent is not None and not significant(by_id[cur_parent]):
            collapsed_ops.append(by_id[cur_parent]["physical_op"])
            cur_parent = by_id[cur_parent]["parent"]
        rec = dict(n)
        rec["display_parent"] = cur_parent
        rec["collapsed_above"] = collapsed_ops
        kept.append(rec)
    return kept


def render_png(nodes, path, title):
    """Reconstructed plan tree (headless box-arrow diagram from the RelOp
    tree -- see module docstring). Node width by self-cost share."""
    if not nodes:
        return
    total_cost = max((n["subtree_cost"] for n in nodes if n["parent"] is None), default=0.0) or 1.0
    nodes = collapse_chains(nodes)
    if not nodes:
        return
    by_parent = {}
    for n in nodes:
        by_parent.setdefault(n["display_parent"], []).append(n)

    xpos = {}
    next_x = [0]

    def assign_x(node_id):
        children = by_parent.get(node_id, [])
        if not children:
            xpos[node_id] = next_x[0]
            next_x[0] += 1
            return xpos[node_id]
        xs = [assign_x(c["id"]) for c in children]
        xpos[node_id] = sum(xs) / len(xs)
        return xpos[node_id]

    roots = by_parent.get(None, [])
    for r in roots:
        assign_x(r["id"])

    by_id = {n["id"]: n for n in nodes}
    display_depth = {}

    def assign_depth(node_id, d):
        display_depth[node_id] = d
        for c in by_parent.get(node_id, []):
            assign_depth(c["id"], d + 1)

    for r in roots:
        assign_depth(r["id"], 0)

    max_depth = max(display_depth.values(), default=0)
    fig_w = max(10, next_x[0] * 1.9)
    fig_h = max(4, (max_depth + 1) * 1.6)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    dominant_id = max(nodes, key=lambda n: n["self_cost"])["id"]

    for n in nodes:
        x, y = xpos[n["id"]], -display_depth[n["id"]]
        pct = 100 * n["self_cost"] / total_cost
        color = "#c0392b" if n["id"] == dominant_id else ("#e67e22" if pct > 10 else "#2980b9")
        box = mpatches.FancyBboxPatch((x - 0.85, y - 0.4), 1.7, 0.8,
                                       boxstyle="round,pad=0.05",
                                       linewidth=1.5, edgecolor=color,
                                       facecolor="white")
        ax.add_patch(box)
        mode_tag = "BATCH" if n["exec_mode"] == "Batch" else ""
        storage_tag = f" [{n['storage']}]" if n["storage"] else ""
        label = f"{n['physical_op']}{storage_tag}\n{pct:.1f}% self-cost{(' · ' + mode_tag) if mode_tag else ''}\n~{n['est_rows']:.0f} rows"
        ax.text(x, y, label, ha="center", va="center", fontsize=8, color=color, weight="bold" if n["id"] == dominant_id else "normal")
        if n["display_parent"] is not None:
            px, py = xpos[n["display_parent"]], -display_depth[n["display_parent"]]
            ax.plot([x, px], [y + 0.4, py - 0.4], color="#999999", linewidth=1, zorder=0)
            if n["collapsed_above"]:
                mx, my = (x + px) / 2, (y + 0.4 + py - 0.4) / 2
                ax.text(mx, my, f"⋯{len(n['collapsed_above'])}⋯", ha="center", va="center",
                        fontsize=6, color="#aaaaaa", style="italic")

    ax.set_xlim(-1.5, next_x[0] - 0.5 + 1.0)
    ax.set_ylim(-(max_depth + 1), 1)
    ax.axis("off")
    ax.set_title(title, fontsize=11, weight="bold")
    fig.text(0.5, 0.01, "Reconstructed from ShowPlan XML (STATISTICS XML ON) -- headless box, not an SSMS screenshot. See .sqlplan for the authoritative artifact.",
              ha="center", fontsize=7, color="#666666")
    fig.tight_layout(rect=[0, 0.03, 1, 1])
    fig.savefig(path, dpi=140)
    plt.close(fig)


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else "before"
    outdir = Path(f"artifacts/tuning/{phase}")
    outdir.mkdir(parents=True, exist_ok=True)

    conn = ld.get_conn()
    cur = conn.cursor()

    summary_lines = [f"=== CAPTURE: {phase.upper()} ===\n"]
    for proc, exec_sql in PROCS.items():
        wall_ms, dmv, stmt_xmls = run_capture(cur, conn, f"dbo.{proc}", exec_sql)
        cpu_ms = dmv["total_worker_time_us"] / 1000.0
        sql_elapsed_ms = dmv["total_elapsed_time_us"] / 1000.0

        all_nodes = []
        for i, xml_str in enumerate(stmt_xmls):
            all_nodes.extend(parse_relops(xml_str, i))
        dominant = max(all_nodes, key=lambda n: n["self_cost"]) if all_nodes else None
        batch_mode_ops = sorted({n["physical_op"] for n in all_nodes if n["exec_mode"] == "Batch"})
        columnstore_ops = sorted({f"{n['physical_op']} [{n['storage']}]" for n in all_nodes if n["storage"]})

        merged = merge_sqlplan(stmt_xmls)
        (outdir / f"{proc}.sqlplan").write_text(merged, encoding="utf-8")

        # render the biggest statement (by node count) -- that's the real query
        stmt_node_counts = {}
        for n in all_nodes:
            stmt_node_counts[n["stmt"]] = stmt_node_counts.get(n["stmt"], 0) + 1
        biggest_stmt = max(stmt_node_counts, key=stmt_node_counts.get) if stmt_node_counts else 0
        render_png([n for n in all_nodes if n["stmt"] == biggest_stmt],
                   outdir / f"{proc}.png", f"{proc} -- {phase} (statement {biggest_stmt})")

        summary_lines.append(f"--- {proc} ---")
        summary_lines.append(f"wall_ms={wall_ms:.1f}  sql_reported_elapsed_ms={sql_elapsed_ms:.1f}  cpu_ms={cpu_ms:.1f}")
        summary_lines.append(f"total_logical_reads (sys.dm_exec_procedure_stats): {dmv['total_logical_reads']}")
        if dominant:
            pct = 100 * dominant["self_cost"] / max(n["subtree_cost"] for n in all_nodes if n["parent"] is None)
            summary_lines.append(f"dominant_operator: {dominant['physical_op']} (logical={dominant['logical_op']}) "
                                  f"self_cost={dominant['self_cost']:.2f} ({pct:.1f}% of statement {dominant['stmt']})")
        summary_lines.append(f"batch_mode_operators: {batch_mode_ops}")
        summary_lines.append(f"columnstore_operators: {columnstore_ops}")
        summary_lines.append("")
        print("\n".join(summary_lines[-8:]))

    (outdir / "summary.txt").write_text("\n".join(summary_lines), encoding="utf-8")
    cur.close()
    conn.close()
    print(f"\nSaved to {outdir}/")


if __name__ == "__main__":
    main()
