# Plum Market Demand Engine

This is a demand engine that decides how much of each item a grocery store should order, built on real supermarket point-of-sale data from Corporacion Favorita in Ecuador. It sizes order-up-to levels item by item from each product's own margin, shelf life, and demand history, then proves the numbers in a 60-day forward simulation. Across 6 stores over that window it cuts the total cost of ordering from $283,734 to $243,427, a saving of $40,307, or about 14%.

"Total cost of ordering" here means the three things bad ordering actually costs you: margin lost when you run out, shrink when food spoils, and the carrying cost of the stock sitting on the shelf.

<img width="1536" height="1024" alt="newsvendor-problem" src="https://github.com/user-attachments/assets/158096e8-f5e5-4457-9096-e48474ed48e8" />

## What the engine does

The core idea is the newsvendor problem in plain terms. A stockout costs you the margin on the sale you missed, because you never bought that unit. Spoilage costs you the whole unit, because you did buy it and then threw it out. So how deep you can afford to stock an item comes down to two things: how much you make per unit, and how long it survives on the shelf. High margin and long shelf life mean you can carry a big cushion. Thin margin and a two-day shelf life mean you can barely carry any.

The engine works this out per item and it shows up cleanly if you look at how many days of extra demand each family's safety stock covers:

| family | shelf life | margin per unit | days of safety stock |
|---|---|---|---|
| Beverages | 270 days | $0.54 | 38.0 |
| Grocery | 365 days | $1.07 | 11.8 |
| Dairy | 14 days | $0.85 | 0.0 |
| Bakery | 2 days | $1.38 | -0.4 |
| Deli | 2 days | $1.89 | -0.7 |
| Produce | 4 days | $0.32 | -1.9 |

The surprising row is Deli, and it is not a rounding artifact. Its safety stock is negative, which means the engine deliberately stocks Deli below expected demand. For fresh prepared food with a two-day shelf life, spoiling a unit costs more than the margin you would have earned selling it, so the profit-maximizing move is to plan to sell out and accept a few missed sales rather than throw food away. Produce goes further in the same direction because its margin is the thinnest in the store.

## Query tuning

This is the part most worth reading, so it goes first.

| proc | before | after | how |
|---|---|---|---|
| usp_RecommendParLevels | 14,994,509 reads, 38.1s | 77,559 reads, 5.9s | decorrelated an OUTER APPLY that re-ran a 1.8M-row aggregate 2,172 times |
| usp_DetectPhantomInventory | 221,402 reads, 4.4s | 114,449 reads, 2.3s | materialized a windowed-expression filter the optimizer estimated at 1 row against 14,673 actual |

The first attempt was the obvious one: add indexes, and when two procs got slower instead of faster, pin them back with hash join hints. It did essentially nothing. Reads on usp_RecommendParLevels moved 0.2%, on usp_DetectPhantomInventory 1.2%. That was the tell. When a "fix" leaves the read count unchanged, the plan did not actually change. The hint had just shoved the plan back to the same shape it already had, including whatever was slow about it in the first place. So the hints came out.

With the hints gone, both real causes turned out to be the same species of problem: the optimizer was handed a wrong row-count estimate and planned accordingly. In usp_RecommendParLevels a correlated OUTER APPLY over a per-item aggregate made SQL Server recompute that whole aggregate once for every store-item, 2,172 times over. Rewriting it as a single LEFT JOIN computed once dropped reads by about 193x. In usp_DetectPhantomInventory the filter sat on a windowed expression, and a window function has no histogram to estimate from, so the optimizer guessed one row against fourteen thousand actual and nested-loop-seeked a 1.8M-row table 124,563 times. Materializing that intermediate result into a temp table gave it real statistics, and it picked a hash join on its own. In both cases, once the estimate was right, the optimizer found the good plan by itself.

On the way there, usp_DetectPhantomInventory got the cheapest hypothesis first: UPDATE STATISTICS WITH FULLSCAN on the suspect table. It changed the read count by exactly zero, from 3,811,333 to 3,811,381. That is what ruled out stale statistics and proved the estimate was structurally wrong rather than just out of date. No hints and no new indexes were needed for either win, and both were checked byte for byte against the original output by checksum before and after.

The third proc, usp_CalculateVelocity, is unchanged, and that is the honest answer. It is not read-bound, it is write-bound: every run deletes and rebuilds all 1.8 million rows of its output table. No index gets under that. The real fix is to refresh it incrementally instead of rebuilding the whole thing each time, and that is a design change, not an indexing change, so it was left alone rather than dressed up.

Full write-up with the plan forensics is in [docs/TUNING.md](docs/TUNING.md).


## What doesn't work, and why

The phantom inventory detector, the piece that tries to flag items where the system thinks there is stock but the shelf is empty, does not work as a yes-or-no detector, and it is worth being blunt about that.

The reason is the base rate. There are 1,958 real phantom events hidden in 1,825,182 store-item-days, which is about 0.1%. At that rarity any fixed global trigger drowns in false alarms. Take a simple global rule, three straight days of zero sales on an item that normally sells around two a day:

```
P(zero sales on a 2-a-day item)     = 13.5%
P(three zero-sale days in a row)     = 0.248%
expected false alarms across panel   = 1,825,182 x 0.00248  =  about 4,500
```

And that is the optimistic version. The detector fights exactly this by setting the bar per item, flagging only when an item's quiet streak runs longer than its own normal quiet spell, so a naturally lumpy item is not held to the same rule as a reliable daily seller. It helps, but only so much: the best honest score as a classifier is still an F1 of 0.26. You cannot threshold your way out of a problem where the thing you are looking for is one in a thousand.

The reframe is what makes it useful. Stop treating it as a detector and treat it as a ranked shortlist. Each morning it hands a store manager the 25 items most likely to be phantom, and about 21% of that list is real. A manager can eyeball a shelf in 30 seconds, so a list that is one-in-five worth checking is genuinely worth walking. What would actually make it a real detector is ground-truth that sales data simply does not contain: cycle counts, shelf cameras, RFID. That is not a gap in this project, it is the reason chains like Walmart and Kroger spend real money on exactly those tools.

## Limitations

- The demand is real Favorita point-of-sale data, but the inventory layer, the on-hand balances and spoilage, is synthetic. No public grocery dataset publishes on-hand inventory, which is itself a big part of why this problem is hard to solve in the wild.
- The simulation runs 60 days, but dry goods have 270 to 365 day shelf lives. Nothing in those families can physically spoil inside the window, so their carrying cost is understated here. You would need a longer horizon to see it.
- Produce clears the bar by only $1,871, and it is capped. The spoilage probability in the model maxes out at 1.0, but produce's true optimum wants to stock even lower than "every safety unit spoils" allows, so roughly 30% of the available saving on produce is left on the table. The engine still wins there, but barely, and by less than it theoretically could.
- Pars are derived once and frozen for the whole 60 days. A real system would re-derive them weekly as demand drifts, so this understates the engine's edge.
- Six stores, and peer groups are derived from store type rather than a richer clustering.

## How it was built

I built this with Claude Code. I set the problem, defined the validation gates, and kept the burden of proof on the numbers, and Claude did a lot of the implementation and analysis against those gates. The SQL and Power BI Report was created by me.

## Stack and how to run it

SQL Server 2022 in Docker, Python 3.12 with pandas and numpy for the data pipeline and simulation, and Power BI for the executive views. The pipeline is a set of numbered scripts in `python/`, the schema and stored procedures are in `sql/`, and the findings are written up in `docs/`.

Power BI report: [https://app.powerbi.com/view?r=eyJrIjoiMzcxMWI5M2YtMGIxZi00MDgyLWFlN2QtNDk2NTI1YjU5NjM2IiwidCI6ImQzNDJlM2MzLTliMDgtNGQyNi04Nzg4LWJiMzA0YjE5YjRiNSJ9&embedImagePlaceholder=true](https://app.powerbi.com/view?r=eyJrIjoiMzcxMWI5M2YtMGIxZi00MDgyLWFlN2QtNDk2NTI1YjU5NjM2IiwidCI6ImQzNDJlM2MzLTliMDgtNGQyNi04Nzg4LWJiMzA0YjE5YjRiNSJ9&embedImagePlaceholder=true)
