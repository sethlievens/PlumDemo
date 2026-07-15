-- =============================================================================
-- Procedure: usp_DetectPhantomInventory
--
-- Purpose
-- -------
-- Flag likely phantom-inventory events: the system shows stock on hand, but
-- nothing has sold in days because the shelf is actually empty (a mislabel, a
-- misplaced pallet, theft). A store-item is flagged only when all four hold:
--
--   a. On-hand is positive (the book says there is stock).
--   b. Zero units sold on the candidate day.
--   c. The current zero-sale streak is longer than THIS item's own normal
--      quiet spell (its historical @percentile zero-run length).
--   d. The same item is still selling normally in peer stores.
--
-- Condition (c) is a per-item threshold, not a global one, and it is the point
-- of the detector. With ~2M store-item-days, a single global "N quiet days is
-- suspicious" bar fires constantly on naturally intermittent items, where a
-- 3-day silence is normal. Asking instead "is this silence unusual FOR THIS
-- ITEM" is what separates a real gap from an item that is simply lumpy.
--
-- Condition (d) keeps this an inference rather than a stockout filter. Flat
-- sales on positive book stock also happen when demand genuinely dies or a
-- vendor stops shipping and every store runs dry at once. Neither is a phantom.
-- If peers are still selling, the demand exists and this store's flatline is
-- local: the empty-shelf case worth walking over to check.
--
-- Inputs
-- ------
-- @scenario_key
--     Scenario to scan.
-- @percentile
--     Per-item baseline percentile for the "normal quiet spell" length. A
--     streak must exceed this to flag. Default 0.95 (95th percentile).
-- @peer_velocity_threshold_pct
--     Peer stores must still be selling at least this fraction of normal for
--     condition (d) to pass. Default 0.60.
-- @run_id (OUTPUT)
--     Run identifier. Auto-assigned as MAX(run_id) + 1 when not supplied.
--
-- Outputs
-- -------
-- Appends one row per detected event to dbo.detection_log (store, item, date,
-- streak length, expected units, confidence score, estimated lost units and
-- revenue). Returns the run_id and inserted row count.
--
-- detection_log is append-only by design: each run keeps its own run_id so
-- threshold sweeps can be compared. The report reads dbo.vw_detection_latest
-- to see only the newest run.
--
-- Process Overview
-- ----------------
-- 1. Measure each item's historical zero-sale runs and its @percentile length.
-- 2. Measure each day's current zero-sale streak.
-- 3. Take candidates whose streak just exceeded their own baseline on positive
--    on-hand (conditions a, b, c).
-- 4. Corroborate against peer stores (condition d) and estimate lost demand.
-- 5. Write surviving candidates with a confidence score.
--
-- Dependencies
-- ------------
-- fact_sales
-- fact_inventory_snap
-- dim_date
-- dim_store
-- dim_item
-- velocity_daily              (populated by usp_CalculateVelocity)
-- detection_log
--
-- Notes
-- -----
-- Peer grouping uses dim_store.peer_group_id (store-type derived), because
-- cluster_id is unique per store in this 6-store subset and would give every
-- store an empty peer group. Candidates with no peer to check still pass, but
-- their confidence score is discounted rather than assumed.
--
-- The per-item baseline is computed once from the whole history rather than
-- point-in-time. Planted phantom events are only ~0.75% of store-item-weeks,
-- so their effect on an item's own zero-run distribution is small, but this is
-- a mild look-ahead simplification worth knowing about.
-- =============================================================================

DROP PROCEDURE IF EXISTS dbo.usp_DetectPhantomInventory;
GO

CREATE PROCEDURE dbo.usp_DetectPhantomInventory
    @scenario_key                  INT,
    @percentile                    DECIMAL(5,4)  = 0.95,
    @peer_velocity_threshold_pct   DECIMAL(5,4)  = 0.60,
    @run_id                        INT           = NULL OUTPUT
AS
BEGIN
    SET NOCOUNT ON;

    SET @run_id = ISNULL(@run_id, (SELECT ISNULL(MAX(run_id), 0) + 1 FROM dbo.detection_log));
    DECLARE @run_ts DATETIME2 = SYSUTCDATETIME();

    -- -------------------------------------------------------------------------
    -- #candidates
    -- Store-items whose current zero-sale streak has just exceeded their own
    -- baseline quiet spell, on positive on-hand (conditions a, b, c). One row
    -- per streak, the first day it crosses the line.
    --
    -- Materialized into a #temp on purpose, as a cardinality fix, and because
    -- the candidate set is referenced three times below. days_dead is a
    -- windowed expression the optimizer cannot estimate; left inline it guesses
    -- ~1 row and nested-loops the downstream joins into millions of reads. The
    -- #temp's own statistics give those joins the real cardinality. See
    -- docs/TUNING.md.
    -- -------------------------------------------------------------------------
    DROP TABLE IF EXISTS #candidates;

    ;WITH daily AS (
        SELECT
            fs.store_key, fs.item_key, fs.date_key, dd.date AS actual_date,
            fs.units_sold, fi.on_hand_units,
            ROW_NUMBER() OVER (PARTITION BY fs.store_key, fs.item_key ORDER BY fs.date_key) AS rn,
            CASE WHEN fs.units_sold = 0 THEN 1 ELSE 0 END AS is_zero
        FROM dbo.fact_sales fs
        JOIN dbo.fact_inventory_snap fi
            ON fi.date_key = fs.date_key AND fi.store_key = fs.store_key
           AND fi.item_key = fs.item_key AND fi.scenario_key = fs.scenario_key
        JOIN dbo.dim_date dd ON dd.date_key = fs.date_key
        WHERE fs.scenario_key = @scenario_key
    ),
    islands AS (
        -- Gaps-and-islands: rn is a true sequential day counter (the data is
        -- fully densified), so days in the same maximal run of a constant
        -- is_zero value share one group id.
        SELECT *, rn - ROW_NUMBER() OVER (PARTITION BY store_key, item_key, is_zero ORDER BY date_key) AS grp
        FROM daily
    ),
    zero_runs AS (
        SELECT store_key, item_key, COUNT(*) AS run_length
        FROM islands
        WHERE is_zero = 1
        GROUP BY store_key, item_key, grp
    ),
    baseline AS (
        -- Each item's "normal" quiet spell: the @percentile of its own
        -- historical zero-run lengths.
        SELECT DISTINCT store_key, item_key,
            PERCENTILE_CONT(@percentile) WITHIN GROUP (ORDER BY CAST(run_length AS FLOAT))
                OVER (PARTITION BY store_key, item_key) AS baseline_zero_run_days
        FROM zero_runs
    ),
    streaks AS (
        -- days_dead = calendar gap since the last day with a real sale.
        -- LAST_VALUE ... IGNORE NULLS carries the most recent selling date
        -- forward across the intervening zero-sale days.
        SELECT
            store_key, item_key, date_key, actual_date, units_sold, on_hand_units,
            DATEDIFF(
                DAY,
                LAST_VALUE(CASE WHEN units_sold > 0 THEN actual_date END) IGNORE NULLS OVER (
                    PARTITION BY store_key, item_key ORDER BY date_key
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ),
                actual_date
            ) AS days_dead
        FROM daily
    )
    -- Fires the first day the streak passes the item's baseline. COALESCE to 0
    -- covers an item with no historical zero-sale day, for which any silence is
    -- unprecedented.
    SELECT s.store_key, s.item_key, s.date_key, s.actual_date, s.days_dead,
           COALESCE(b.baseline_zero_run_days, 0) AS baseline_zero_run_days
    INTO #candidates
    FROM streaks s
    LEFT JOIN baseline b ON b.store_key = s.store_key AND b.item_key = s.item_key
    WHERE s.units_sold = 0
      AND s.on_hand_units > 0
      AND s.days_dead = FLOOR(COALESCE(b.baseline_zero_run_days, 0)) + 1;

    ;WITH

    -- -------------------------------------------------------------------------
    -- expected_window
    -- Estimated demand lost over the flagged window (clean baseline times
    -- weekday index, summed). The window length varies per item because the
    -- threshold is adaptive. COALESCE guards a window whose days are all NULL.
    -- -------------------------------------------------------------------------
    expected_window AS (
        SELECT
            c.store_key, c.item_key, c.date_key,
            COALESCE(SUM(v.trailing_28d_mean_units * v.dow_index), 0) AS expected_units
        FROM #candidates c
        JOIN dbo.velocity_daily v
            ON v.store_key = c.store_key AND v.item_key = c.item_key AND v.scenario_key = @scenario_key
        JOIN dbo.dim_date vd ON vd.date_key = v.date_key
        WHERE vd.date BETWEEN DATEADD(DAY, -(c.days_dead - 1), c.actual_date) AND c.actual_date
        GROUP BY c.store_key, c.item_key, c.date_key
    ),

    -- -------------------------------------------------------------------------
    -- peer_check
    -- Condition (d): peers' current activity (7-day actual) against their own
    -- baseline (28-day mean), averaged across the peer group, excluding this
    -- store. NULL when the store has no peer to compare against.
    -- -------------------------------------------------------------------------
    peer_check AS (
        SELECT
            c.store_key, c.item_key, c.date_key,
            AVG(peer_v.trailing_7d_actual_units / NULLIF(peer_v.trailing_28d_mean_units, 0)) AS peer_velocity_ratio
        FROM #candidates c
        JOIN dbo.dim_store my_store ON my_store.store_key = c.store_key
        JOIN dbo.dim_store peer_store
            ON peer_store.peer_group_id = my_store.peer_group_id AND peer_store.store_key <> my_store.store_key
        JOIN dbo.velocity_daily peer_v
            ON peer_v.store_key = peer_store.store_key AND peer_v.item_key = c.item_key
           AND peer_v.date_key = c.date_key AND peer_v.scenario_key = @scenario_key
        GROUP BY c.store_key, c.item_key, c.date_key
    )

    INSERT INTO dbo.detection_log
        (run_id, run_ts, store_key, item_key, detected_date, days_dead,
         expected_units, confidence_score, est_lost_units, est_lost_revenue)
    SELECT
        @run_id, @run_ts, c.store_key, c.item_key, c.actual_date, c.days_dead,
        ew.expected_units,
        -- Weighted confidence (each term capped at 1.0): peer corroboration
        -- (discounted to 0.7 when there is no peer to check) and how far the
        -- streak has run past this item's own baseline (a ratio, since a normal
        -- streak length varies widely by item).
        ROUND(
              0.5 * (CASE WHEN pc.peer_velocity_ratio IS NULL THEN 0.7
                          ELSE LEAST(pc.peer_velocity_ratio / @peer_velocity_threshold_pct, 1.0) END)
            + 0.5 * (CASE WHEN c.baseline_zero_run_days = 0 THEN 1.0
                          ELSE LEAST(CAST(c.days_dead AS DECIMAL(10,4)) / c.baseline_zero_run_days / 2.0, 1.0) END)
        , 4) AS confidence_score,
        ew.expected_units AS est_lost_units,
        ROUND(ew.expected_units * di.retail_price, 2) AS est_lost_revenue
    FROM #candidates c
    JOIN expected_window ew
        ON ew.store_key = c.store_key AND ew.item_key = c.item_key AND ew.date_key = c.date_key
    LEFT JOIN peer_check pc
        ON pc.store_key = c.store_key AND pc.item_key = c.item_key AND pc.date_key = c.date_key
    JOIN dbo.dim_item di ON di.item_key = c.item_key
    WHERE pc.peer_velocity_ratio IS NULL OR pc.peer_velocity_ratio >= @peer_velocity_threshold_pct;

    DECLARE @rows INT = @@ROWCOUNT;
    DROP TABLE IF EXISTS #candidates;
    SELECT @run_id AS run_id, @rows AS rows_inserted;
END
GO
