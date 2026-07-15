# Grocery Store Demand Engine

A demand planning engine for grocery retail that recommends optimal order-up-to (par) levels from item-level economics and validates them with a forward simulation.

Built from real supermarket point-of-sale data from **Corporación Favorita** (Ecuador), the engine combines demand forecasting, inventory simulation, and SQL optimization to answer a simple business question:

> **How much of every item should a store stock to maximize profit while minimizing waste?**

Instead of assigning one safety-stock rule to every product, the engine derives each item's order level from its own demand history, margin, shelf life, and replenishment cadence.

---

## Project Highlights

- Designed a SQL Server data warehouse with ~2.4 million fact rows.
- Implemented inventory optimization using the Newsvendor model.
- Built a phantom inventory detection system using historical sales patterns.
- Tuned analytical stored procedures, reducing runtime by up to 84% and logical reads by 99.5%.
- Verified all optimizations using execution plans and checksum-based correctness testing.

---

## Results

Across a 60-day forward simulation covering six stores:

| Metric | Baseline | Demand Engine |
|--------|----------:|--------------:|
| Total Cost of Ordering | **$283,734** | **$243,427** |
| Improvement | | **$40,307 saved (14%)** |

The engine reduces the **total cost of ordering**, defined as:

- Lost margin from stockouts
- Food spoilage
- Inventory carrying cost

while maintaining high service levels.

---

# The business problem

Every grocery item has a different economic tradeoff.

Running out of milk costs a sale.

Throwing away prepared deli food costs the entire product.

Those costs are rarely equal.

Traditional inventory systems often apply broad safety-stock rules to entire product categories. This project instead treats every SKU as its own optimization problem using the classic **Newsvendor model**.

The engine calculates the optimal amount of safety stock by balancing:

- expected demand
- profit per unit
- shelf life
- demand variability
- replenishment cadence

The result is a different stocking strategy for every item.

<img width="1536" height="1024" alt="newsvendor-problem" src="https://github.com/user-attachments/assets/158096e8-f5e5-4457-9096-e48474ed48e8" />

---

# What the engine learned

The behavior that emerged matched real grocery economics.

| Family | Shelf Life | Margin / Unit | Days of Safety Stock |
|---|---:|---:|---:|
| Beverages | 270 days | $0.54 | 38.0 |
| Grocery | 365 days | $1.07 | 11.8 |
| Dairy | 14 days | $0.85 | 0.0 |
| Bakery | 2 days | $1.38 | -0.4 |
| Deli | 2 days | $1.89 | -0.7 |
| Produce | 4 days | $0.32 | -1.9 |

The most interesting result is **negative safety stock**.

For fresh prepared foods, spoilage is so expensive that deliberately planning to sell out before closing can produce a higher long-term profit than keeping shelves completely full.

The engine therefore recommends stocking **below expected demand** for some highly perishable products.

That behavior was not hard-coded—it emerged directly from the underlying economics.

---

# Query optimization

One unexpected finding was that **indexes were not the primary performance bottleneck**.

The largest improvements came from fixing poor execution plans caused by incorrect cardinality estimates.

| Procedure | Before | After | Improvement |
|-----------|-------:|------:|------------:|
| `usp_RecommendParLevels` | 14,994,509 reads<br>38.1 s | 77,559 reads<br>5.9 s | **193× fewer reads** |
| `usp_DetectPhantomInventory` | 221,402 reads<br>4.4 s | 114,449 reads<br>2.3 s | **48% fewer reads** |

### `usp_RecommendParLevels`

A correlated `OUTER APPLY` caused SQL Server to recompute the same aggregate once for every store-item combination (2,172 executions).

Rewriting the logic as a single `LEFT JOIN` allowed the optimizer to compute the aggregate once and reuse it across the query.

### `usp_DetectPhantomInventory`

A predicate depended on a window function, which has no histogram statistics.

SQL Server estimated one qualifying row when more than **14,000** actually matched, producing an inefficient nested-loop plan.

Materializing the intermediate result into a temporary table gave the optimizer accurate statistics, after which it naturally selected a hash join.

### What didn't help

The first attempt was simply rebuilding statistics using `UPDATE STATISTICS ... WITH FULLSCAN`.

Logical reads were effectively unchanged, proving the problem wasn't stale statistics—it was inaccurate cardinality estimation.

No additional indexes were required for either optimization.

---

# Phantom inventory detection

One goal of the project was detecting **phantom inventory**:

Products that the inventory system believes are on the shelf even though the shelf is actually empty.

The original idea was straightforward:

- identify products with several consecutive days of zero sales
- compare them against similar stores
- flag suspicious items

The challenge turned out to be statistical.

The data contains:

- **1,958** true phantom events
- **1,825,182** store-item-days

Only **0.1%** of observations are actual phantom events.

At that rarity, even a good classifier produces many false positives.

The detector ultimately achieved roughly **21% precision** when ranking the top candidate shelves.

Rather than treating it as a binary classifier, it became far more useful as a daily inspection list.

Each morning the system presents the **25 shelves most likely to contain phantom inventory**.

A store manager can visually inspect each shelf in seconds, making a one-in-five success rate operationally worthwhile.

The project also demonstrates why large retailers increasingly rely on physical sensing technologies such as:

- cycle counts
- shelf cameras
- RFID

Those systems provide information that transaction history alone simply cannot.

---

# Project limitations

- Sales data comes from the real Favorita dataset, but inventory levels and spoilage are simulated because public grocery datasets do not include on-hand inventory.
- The forward simulation covers 60 days. Products with 270–365 day shelf lives cannot physically expire during that period, so long-term carrying costs are understated.
- Produce remains the hardest category. The model still improves performance, but its theoretical optimum lies beyond the current spoilage model's constraints.
- Recommended par levels remain fixed throughout the simulation. A production system would recalculate them continuously.
- The project models six stores, with peer groups derived from store type rather than richer clustering techniques.

---

# Technology

- SQL Server 2022
- Python 3.12
- pandas
- NumPy
- Power BI
- Docker

---

# Development approach

The data pipeline and simulation framework were developed collaboratively with Claude Code.

I defined the business problem, designed the validation methodology, established quantitative acceptance criteria, and continuously challenged intermediate results until they matched the underlying inventory economics.

The SQL implementation, stored procedures, query tuning, and Power BI reporting were designed and built by me.

---

# Dashboard

Power BI report:

[https://app.powerbi.com/view?r=eyJrIjoiMzcxMWI5M2YtMGIxZi00MDgyLWFlN2QtNDk2NTI1YjU5NjM2IiwidCI6ImQzNDJlM2MzLTliMDgtNGQyNi04Nzg4LWJiMzA0YjE5YjRiNSJ9&embedImagePlaceholder=true](https://app.powerbi.com/view?r=eyJrIjoiMzcxMWI5M2YtMGIxZi00MDgyLWFlN2QtNDk2NTI1YjU5NjM2IiwidCI6ImQzNDJlM2MzLTliMDgtNGQyNi04Nzg4LWJiMzA0YjE5YjRiNSJ9&embedImagePlaceholder=true)
