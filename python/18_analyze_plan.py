"""
Plan forensics -- answers the four diagnostic questions per proc from a saved
.sqlplan, using the ACTUAL run-time counters (RunTimeInformation), not the
optimizer's estimates alone:

  1. Is a columnstore index scan present at all? (Storage="ColumnStore")
  2. Execution mode per operator: Batch vs Row (batch is the point of columnstore).
  3. Single most expensive operator by % of statement subtree cost (self-cost).
  4. Operators where actual rows differ from estimated by >10x (cardinality skew --
     the usual root cause of a scan-per-row nested loop).

Usage: python/18_analyze_plan.py artifacts/tuning/after/<proc>.sqlplan
"""

import sys
import xml.etree.ElementTree as ET

NS = {"sp": "http://schemas.microsoft.com/sqlserver/2004/07/showplan"}


def runtime(relop):
    """Summed ActualRows across threads, and max ActualExecutions."""
    rows = execs = 0
    seen = False
    rti = relop.find("./sp:RunTimeInformation", NS)
    if rti is not None:
        for t in rti.findall("./sp:RunTimeCountersPerThread", NS):
            rows += int(t.get("ActualRows", 0))
            execs = max(execs, int(t.get("ActualExecutions", 1)))
            seen = True
    return (rows, execs) if seen else (None, None)


def storage_of(relop):
    for tag in ("IndexScan", "IndexSeek", "ColumnStoreIndexScan", "TableScan"):
        el = relop.find(f"sp:{tag}", NS)
        if el is not None:
            obj = el.find("sp:Object", NS)
            idx = obj.get("Index") if obj is not None else None
            return el.get("Storage"), idx
    return None, None


def walk(relop, depth, parent_subtree, out):
    subtree = float(relop.get("EstimatedTotalSubtreeCost", 0))
    # self cost = subtree - sum(children subtree)
    child_relops = []
    for wrapper in list(relop):
        if wrapper.tag.endswith("}RelOp"):
            continue
        child_relops.extend(wrapper.findall("sp:RelOp", NS))
    child_sum = sum(float(c.get("EstimatedTotalSubtreeCost", 0)) for c in child_relops)
    self_cost = max(0.0, subtree - child_sum)
    act_rows, act_execs = runtime(relop)
    est_rows = float(relop.get("EstimateRows", 0))
    # per-execution estimate; actual is summed across all executions
    est_total = est_rows * (act_execs if act_execs else 1)
    storage, idx = storage_of(relop)
    out.append(dict(
        depth=depth, op=relop.get("PhysicalOp"), logical=relop.get("LogicalOp"),
        mode=relop.get("EstimatedExecutionMode", "Row"),
        self_cost=self_cost, subtree=subtree,
        est_rows_per_exec=est_rows, est_total=est_total,
        act_rows=act_rows, act_execs=act_execs,
        storage=storage, index=idx,
    ))
    for c in child_relops:
        walk(c, depth + 1, subtree, out)


def analyze(path):
    root = ET.parse(path).getroot()
    stmts = root.findall(".//sp:StmtSimple", NS)
    print(f"\n{'='*78}\n{path}\n{'='*78}")
    all_nodes = []
    for si, stmt in enumerate(stmts):
        top = stmt.find("./sp:QueryPlan/sp:RelOp", NS)
        if top is None:
            continue
        nodes = []
        walk(top, 0, None, nodes)
        for n in nodes:
            n["stmt"] = si
        all_nodes.extend(nodes)

    # Q1: columnstore scans
    cs = [n for n in all_nodes if n["storage"] == "ColumnStore"]
    print("\n[Q1] Columnstore index scans present:", "YES" if cs else "NO")
    for n in cs:
        print(f"      stmt{n['stmt']} {n['op']} on {n['index']} "
              f"mode={n['mode']} act_rows={n['act_rows']}")

    # Q2: batch vs row
    batch = [n for n in all_nodes if n["mode"] == "Batch"]
    row = [n for n in all_nodes if n["mode"] == "Row"]
    print(f"\n[Q2] Execution mode: {len(batch)} Batch operators, {len(row)} Row operators")
    batch_ops = sorted({n["op"] for n in batch})
    print(f"      Batch-mode ops: {batch_ops}")

    # Q3: most expensive operator per statement, by self-cost share
    print("\n[Q3] Most expensive operator (self-cost) per statement:")
    for si in sorted({n["stmt"] for n in all_nodes}):
        snodes = [n for n in all_nodes if n["stmt"] == si]
        root_cost = max((n["subtree"] for n in snodes if n["depth"] == 0), default=1) or 1
        top = max(snodes, key=lambda n: n["self_cost"])
        pct = 100 * top["self_cost"] / root_cost
        print(f"      stmt{si}: {top['op']} ({top['logical']}) "
              f"{pct:.1f}% self-cost, mode={top['mode']}"
              f"{', on '+str(top['index']) if top['index'] else ''}")

    # Q4: est vs actual skew > 10x
    print("\n[Q4] Operators with actual-vs-estimate row skew > 10x:")
    skewed = []
    for n in all_nodes:
        if n["act_rows"] is None:
            continue
        est = max(n["est_total"], 0.5)
        act = n["act_rows"]
        if act == 0:
            continue
        ratio = act / est
        if ratio > 10 or ratio < 0.1:
            skewed.append((ratio, n))
    skewed.sort(key=lambda x: -abs(x[0]))
    if not skewed:
        print("      (none)")
    for ratio, n in skewed[:15]:
        print(f"      stmt{n['stmt']} d{n['depth']:>2} {n['op']:22s} "
              f"est_total={n['est_total']:>12.0f} act={n['act_rows']:>12} "
              f"execs={n['act_execs']:>9} skew={ratio:>8.1f}x"
              f"{'  <'+str(n['index'])+'>' if n['index'] else ''}")

    # bonus: biggest read-drivers (highest actual_executions -> scan-per-row tells)
    print("\n[bonus] Highest actual_executions (scan-per-row indicator):")
    by_exec = sorted([n for n in all_nodes if n["act_execs"]], key=lambda n: -n["act_execs"])
    for n in by_exec[:6]:
        print(f"      stmt{n['stmt']} {n['op']:22s} execs={n['act_execs']:>10} "
              f"act_rows={n['act_rows']:>12}"
              f"{'  <'+str(n['index'])+'>' if n['index'] else ''}")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        analyze(p)
