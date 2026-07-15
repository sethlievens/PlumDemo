# Query Performance Tuning

## Overview

This project includes a second round of performance tuning that combines **physical database design** (indexing) with **execution plan analysis** and **query rewrites**.

The objective was to improve the stored procedures performance while preserving output.

---

# Round 2 Indexing

The baseline schema intentionally contains only clustered primary keys to establish a realistic "before" state.

The second tuning pass introduced:

- Clustered columnstore index on `fact_sales`
- Covering nonclustered index for seek-heavy access patterns
- Reclustered `fact_inventory_snap` on its primary join keys
- Filtered index for promotional sales

These changes were applied without modifying any data or stored procedure logic.

---

# Performance Analysis

Execution plans were captured before and after each change.

Measurements included:

- Execution time
- Logical reads
- Actual execution plans
- Operator execution counts
- Estimated vs. actual row counts

Correctness was verified after every optimization using checksums to ensure identical output.

---

# Findings

The indexing improvements produced mixed results.

| Procedure | Result |
|-----------|--------|
| `usp_CalculateVelocity` | Essentially unchanged |
| `usp_DetectPhantomInventory` | Regression |
| `usp_RecommendParLevels` | Significant regression |

Execution plan analysis showed that the regressions were **not caused by the indexes themselves**, but by pre-existing cardinality estimation problems that became more visible after the optimizer selected different execution strategies.

---

# Root Cause Analysis

## 1. Correlated `OUTER APPLY`

`usp_RecommendParLevels` repeatedly recomputed an expensive aggregate once per store/item combination.

Replacing the correlated `OUTER APPLY` with a single `LEFT JOIN` allowed SQL Server to compute the aggregation once and reuse the results.

### Result

- **Logical reads:** 14,994,509 → **77,559** (~193× reduction)
- **Execution time:** 38.1 s → **5.9 s**
- Output verified identical using checksums

---

## 2. Cardinality Estimation Failure

`usp_DetectPhantomInventory` filtered on a window-function result that SQL Server could not accurately estimate.

The optimizer estimated approximately one row when the actual cardinality was roughly 14,000 rows, resulting in an inefficient nested-loop plan.

Materializing the intermediate result into a temporary table allowed SQL Server to generate accurate statistics and choose a hash join instead.

### Result

- **Logical reads:** 221,402 → **114,449** (48% reduction)
- **Execution time:** 4.4 s → **2.3 s**
- Output verified identical using checksums

---

# Why Indexing Alone Wasn't Enough

One of the most interesting findings from this project was that adding indexes alone did **not** automatically improve performance.

Although the new indexes reduced the cost of individual scans, the optimizer selected different execution plans that exposed underlying query-shape issues.

The largest performance gains ultimately came from improving cardinality estimation and reducing unnecessary work rather than adding more indexes or forcing join hints.

---

# Final Results

| Procedure | Round 1 Reads | Final Reads | Round 1 Time | Final Time |
|-----------|--------------:|------------:|-------------:|-----------:|
| `usp_CalculateVelocity` | 1,092,976 | 1,079,736 | 45.9 s | 45.5 s |
| `usp_DetectPhantomInventory` | 221,402 | **114,449** | 4.4 s | **2.3 s** |
| `usp_RecommendParLevels` | 14,994,509 | **77,559** | 38.1 s | **5.9 s** |

---

# Key Takeaways

- Designed and evaluated multiple indexing strategies.
- Used actual execution plans to diagnose optimizer behavior.
- Compared estimated versus actual row counts to identify cardinality estimation problems.
- Improved performance through query rewrites instead of optimizer hints.
- Verified every optimization produced identical results using checksums.
- Demonstrated practical SQL Server performance tuning techniques involving indexing, execution plans, and query optimization.
