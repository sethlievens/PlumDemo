# Plum Market Demand Engine

This is a demand engine that decides how much of each item a grocery store should order, built on real supermarket point-of-sale data from Corporacion Favorita in Ecuador. It sizes order-up-to levels item by item from each product's own margin, shelf life, and demand history, then proves the numbers in a 60-day forward simulation. Across 6 stores over that window it cuts the total cost of ordering from $283,734 to $243,427, a saving of $40,307, or about 14%.

"Total cost of ordering" here means the three things bad ordering actually costs you: margin lost when you run out, shrink when food spoils, and the carrying cost of the stock sitting on the shelf.

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

## Bugs found and fixed

Finding these was the work, so they are worth listing.

**Demand seeded off a promo-contaminated velocity.** The forward simulation seeded each item's demand from its raw trailing 28-day average. One Grocery item had been on promotion nearly every day of that window, selling up to 2,305 units a day against a clean baseline of 14.4. So the simulation started that item's "normal" demand at a promo spike, which is self-contradictory because the simulation holds promotions flat at their off state. Caught by tracing an item whose demand looked absurd, and fixed by seeding off the clean average that already excludes promo and stockout days.

**Stale velocity on discontinued items.** The "most recent" velocity for an item had no floor on how old it could be. A Beverages item at store 1 got seeded at 145.7 units a day from a reading whose last real sale was seven months before the simulation even starts. Across the panel, 144 store-items out of 2,175, about 6.6%, had last sold more than 28 days before the start date. They were showing up as some of the biggest lost-revenue outliers. Fixed by dropping any store-item that had gone dark for more than 28 days.

**The day-of-week index collapsed on promo-heavy days.** Deli's Friday index came out dead last at 0.37, even though Friday is genuinely the third-strongest sales day. The cause was that these Deli items are on promotion 93% of Fridays in the source data, and the index was built from promo-excluded sales, so excluding promo threw away almost every Friday and left an unrepresentative scrap. The simulation then generated almost no Friday demand while orders still arrived for it, and a two-day shelf life dumped the surplus into Saturday. The fix was recognizing that the level of demand and the shape of demand need different filters: the level should exclude promo so a spike does not inflate reorder sizes, but the day-of-week shape should keep promo, because the traffic on a busy Friday is real. After the fix, Deli Friday demand went from 2,131 units to 7,737 and Saturday shrink dropped from 33% to 4.9%.

**Safety stock priced at the average spoilage rate instead of the tail rate.** Safety stock sits behind cycle stock in a first-in-first-out shelf, so it only ever sells on the unusual high-demand days, which means it spoils far more often than an average unit. The model was pricing its spoilage risk at the average rate. This one was caught by sweeping the actual cost objective across a range of stocking levels and noticing the engine's own recommended level sat 60% above the cost minimum it was supposed to be finding. Fixed with a per-family spoilage correction that was then validated by fitting it on three stores and checking it on the other three.

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

Being straight about the edges of this.

- The demand is real Favorita point-of-sale data, but the inventory layer, the on-hand balances and spoilage, is synthetic. No public grocery dataset publishes on-hand inventory, which is itself a big part of why this problem is hard to solve in the wild.
- The simulation runs 60 days, but dry goods have 270 to 365 day shelf lives. Nothing in those families can physically spoil inside the window, so their carrying cost is understated here. You would need a longer horizon to see it.
- Produce clears the bar by only $1,871, and it is capped. The spoilage probability in the model maxes out at 1.0, but produce's true optimum wants to stock even lower than "every safety unit spoils" allows, so roughly 30% of the available saving on produce is left on the table. The engine still wins there, but barely, and by less than it theoretically could.
- Pars are derived once and frozen for the whole 60 days. A real system would re-derive them weekly as demand drifts, so this understates the engine's edge.
- Six stores, and peer groups are derived from store type rather than a richer clustering.

## How it was built

I built this with Claude Code as an engineering partner. I set the problem, defined the validation gates, and kept the burden of proof on the numbers, and Claude did a lot of the implementation and analysis against those gates. Several of the bugs above were caught precisely because a result looked plausible and still had to survive a check, the promo-contaminated demand, the collapsed Friday index, and the mispriced safety stock all came out of refusing to accept a number that looked fine on the surface.

## Stack and how to run it

SQL Server 2022 in Docker, Python 3.12 with pandas and numpy for the data pipeline and simulation, and Power BI for the executive views. The pipeline is a set of numbered scripts in `python/`, the schema and stored procedures are in `sql/`, and the findings are written up in `docs/`.

Power BI report: TODO, add public report link.
