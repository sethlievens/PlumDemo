-- =============================================================================
-- Procedure: usp_RecommendParLevels
--
-- Purpose
-- -------
-- Recommend an order-up-to level ("par") for every Store + Item, sized from
-- that item's own economics rather than a one-size-fits-all service target.
--
-- This is a per-item newsvendor calculation. It balances the cost of running
-- out (lost margin) against the cost of overstocking (carrying + spoilage),
-- and caps the result so it never recommends more than an item can sell
-- before it expires.
--
--     par          = clamp(0, expected_demand + safety_stock,
--                             velocity * shelf_life_days)
--     safety_stock = sigma_daily * z * SQRT(W)
--     z            = NormInv( Cu / (Cu + Co) )
--     Cu           = retail_price - unit_cost              -- cost of running out
--     Co           = carrying_cost + P(spoil) * unit_cost  -- cost of overstock
--     W            = 2 * cadence_days                       -- protection window
--
-- z can come out NEGATIVE. That is intentional: for a thin-margin, short-shelf
-- item, avoiding spoilage is worth more than avoiding a stockout, so the model
-- deliberately stocks below expected demand.
--
-- Inputs
-- ------
-- @source_scenario_key
--     Scenario whose sales history and velocity feed the calculation.
-- @output_scenario_key
--     Scenario to write recommendations into. Defaults to 'Engine Recommended'.
-- @effective_date
--     "As of" date for the recommendation. Defaults to the latest date in the
--     source scenario's history.
-- @family_filter
--     Optional single-family scope. NULL processes every family.
-- @override_z
--     NULL derives z per item (the real recommendation). A supplied value
--     forces a uniform z, used only to generate the naive comparison scenarios.
-- @carrying_annual_rate
--     Annual carrying rate (capital + shelf space + shrink risk). Default 0.25.
--
-- Outputs
-- -------
-- Replaces this scenario's rows in dbo.par_levels with, per Store + Item:
--   • par_units, reorder_point, safety_stock_units
--   • critical_ratio, derived_z, derived_service_level, days_of_safety_stock
--   • projected_spoilage_units, projected_stockout_units
-- Returns the output scenario, effective date, and inserted row count.
--
-- Process Overview
-- ----------------
-- 1. Measure demand variability per Store + Item (the safety-stock sigma).
-- 2. Take each item's most recent 28-day velocity.
-- 3. Project expected demand across the protection window, weekday-adjusted.
-- 4. Derive the newsvendor z from each item's understock / overstock costs.
-- 5. Size safety stock and the final par, capped at shelf-life capacity.
--
-- Dependencies
-- ------------
-- fact_sales
-- fact_inventory_snap
-- dim_date
-- dim_item
-- dim_scenario
-- dim_spoilage_calibration
-- velocity_daily              (populated by usp_CalculateVelocity)
-- ufn_NormInv, ufn_NormSDist
-- =============================================================================

DROP PROCEDURE IF EXISTS dbo.usp_RecommendParLevels;
GO

CREATE PROCEDURE dbo.usp_RecommendParLevels
    @source_scenario_key     INT,
    @output_scenario_key     INT           = NULL,
    @effective_date          DATE          = NULL,
    @family_filter           VARCHAR(50)   = NULL,
    @override_z              DECIMAL(5,4)  = NULL,
    @carrying_annual_rate    DECIMAL(5,4)  = 0.25
AS
BEGIN
    SET NOCOUNT ON;

    IF @output_scenario_key IS NULL
        SELECT @output_scenario_key = scenario_key
        FROM dbo.dim_scenario
        WHERE scenario_name = 'Engine Recommended';

    IF @effective_date IS NULL
        SELECT @effective_date = MAX(dd.date)
        FROM dbo.fact_sales fs
        JOIN dbo.dim_date dd ON dd.date_key = fs.date_key
        WHERE fs.scenario_key = @source_scenario_key;

    -- par_levels holds a current recommendation, not run history, so clear
    -- this scenario's rows (scoped to the family filter) before rebuilding.
    DELETE pl
    FROM dbo.par_levels pl
    JOIN dbo.dim_item di ON di.item_key = pl.item_key
    WHERE pl.scenario_key = @output_scenario_key
      AND (@family_filter IS NULL OR di.family = @family_filter);

    ;WITH

    -- -------------------------------------------------------------------------
    -- demand_stats
    -- Day-to-day demand variability per Store + Item, which sizes safety stock.
    -- Promo and stockout days are excluded because neither reflects normal
    -- demand: a promo inflates it, a stockout zeroes it for lack of supply.
    -- -------------------------------------------------------------------------
    demand_stats AS (
        SELECT
            fs.store_key, fs.item_key,
            STDEV(fs.units_sold) AS sigma_daily_demand
        FROM dbo.fact_sales fs
        JOIN dbo.fact_inventory_snap fi
            ON fi.date_key = fs.date_key AND fi.store_key = fs.store_key
           AND fi.item_key = fs.item_key AND fi.scenario_key = fs.scenario_key
        JOIN dbo.dim_date dd ON dd.date_key = fs.date_key
        WHERE fs.scenario_key = @source_scenario_key
          AND fs.on_promo_flag = 0 AND fi.on_hand_units > 0
          AND dd.date <= @effective_date
        GROUP BY fs.store_key, fs.item_key
    ),

    -- -------------------------------------------------------------------------
    -- latest_velocity
    -- The single most recent 28-day average per Store + Item as of the
    -- effective date. Filtered to non-NULL and ranked newest-first so a promo
    -- or stockout day (whose average is NULL) can't become the anchor and
    -- propagate NULLs downstream into a failed insert.
    -- -------------------------------------------------------------------------
    latest_velocity AS (
        SELECT store_key, item_key, trailing_28d_mean_units
        FROM (
            SELECT store_key, item_key, trailing_28d_mean_units,
                   ROW_NUMBER() OVER (PARTITION BY store_key, item_key ORDER BY date_key DESC) AS rn
            FROM dbo.velocity_daily
            WHERE scenario_key = @source_scenario_key
              AND date_key <= CONVERT(INT, FORMAT(@effective_date, 'yyyyMMdd'))
              AND trailing_28d_mean_units IS NOT NULL
        ) ranked
        WHERE rn = 1
    ),

    -- -------------------------------------------------------------------------
    -- dow_profile
    -- Each Store + Item's average sales multiplier by day of week, used to
    -- shape flat expected demand toward busier and quieter weekdays.
    -- -------------------------------------------------------------------------
    dow_profile AS (
        SELECT v.store_key, v.item_key, dd.day_of_week, AVG(v.dow_index) AS avg_dow_index
        FROM dbo.velocity_daily v
        JOIN dbo.dim_date dd ON dd.date_key = v.date_key
        WHERE v.scenario_key = @source_scenario_key
        GROUP BY v.store_key, v.item_key, dd.day_of_week
    ),

    -- -------------------------------------------------------------------------
    -- tally / cycle_days
    -- Enumerate every calendar day inside each item's protection window
    -- (2 * cadence_days) and label it with its weekday, so expected demand can
    -- be summed weekday-by-weekday across the window. DATENAME is used directly
    -- because these are future dates beyond dim_date's history.
    -- -------------------------------------------------------------------------
    tally AS (
        SELECT value AS n FROM GENERATE_SERIES(1, 31)
    ),
    cycle_days AS (
        SELECT di.item_key, DATENAME(WEEKDAY, DATEADD(DAY, t.n, @effective_date)) AS cycle_dow
        FROM dbo.dim_item di
        CROSS JOIN tally t
        WHERE t.n <= 2 * di.cadence_days
    ),

    -- -------------------------------------------------------------------------
    -- expected_demand
    -- Total expected units over the protection window: velocity times each
    -- upcoming day's weekday multiplier, summed per Store + Item.
    -- -------------------------------------------------------------------------
    expected_demand AS (
        SELECT
            lv.store_key, lv.item_key,
            MAX(lv.trailing_28d_mean_units) AS trailing_28d_mean_units,
            SUM(lv.trailing_28d_mean_units * COALESCE(dp.avg_dow_index, 1.0)) AS expected_demand_over_cycle
        FROM latest_velocity lv
        JOIN cycle_days cd ON cd.item_key = lv.item_key
        LEFT JOIN dow_profile dp
            ON dp.store_key = lv.store_key AND dp.item_key = lv.item_key AND dp.day_of_week = cd.cycle_dow
        GROUP BY lv.store_key, lv.item_key
    )

    INSERT INTO dbo.par_levels
        (store_key, item_key, scenario_key, par_units, reorder_point, safety_stock_units,
         effective_date, projected_spoilage_units, projected_stockout_units,
         critical_ratio, derived_z, derived_service_level, days_of_safety_stock)
    SELECT
        ed.store_key, ed.item_key, @output_scenario_key,
        capped.par_units,
        stock.safety_stock_units AS reorder_point,
        stock.safety_stock_units,
        @effective_date,
        -- Spoilage exposure: par held above expected cycle demand.
        GREATEST(0, capped.par_units - ed.expected_demand_over_cycle)                              AS projected_spoilage_units,
        -- Stockout exposure: desired cover the shelf-life cap couldn't fund.
        GREATEST(0, (ed.expected_demand_over_cycle + stock.safety_stock_units) - capped.par_units) AS projected_stockout_units,
        w.cu / NULLIF(w.cu + ov.co, 0)  AS critical_ratio,
        zc.z                            AS derived_z,
        dbo.ufn_NormSDist(zc.z)         AS derived_service_level,
        stock.safety_stock_units / NULLIF(ed.trailing_28d_mean_units, 0) AS days_of_safety_stock
    FROM expected_demand ed
    JOIN dbo.dim_item di ON di.item_key = ed.item_key
        AND (@family_filter IS NULL OR di.family = @family_filter)
    LEFT JOIN dbo.dim_spoilage_calibration sc ON sc.family = di.family
    -- demand_stats is 1:1 with the outer row set, so it joins once here rather
    -- than as a correlated APPLY. That decorrelation is the key performance
    -- fix for this proc (see docs/TUNING.md); LEFT JOIN + COALESCE below keeps
    -- the "no history, sigma = 0" fallback intact.
    LEFT JOIN demand_stats ds ON ds.store_key = ed.store_key AND ds.item_key = ed.item_key

    -- Protection window and understock cost (lost margin per missed unit).
    CROSS APPLY (
        SELECT
            2.0 * di.cadence_days AS protection_window_days,
            (di.retail_price - di.unit_cost) AS cu
    ) w

    -- Overstock cost: carrying cost over the window plus expected spoilage.
    -- P(spoil) decays with shelf life relative to the window and is scaled by
    -- the family's empirical spoilage multiplier (default 1.0), capped at 1.0.
    CROSS APPLY (
        SELECT
            di.unit_cost * @carrying_annual_rate * (w.protection_window_days / 365.0)
            + LEAST(1.0, COALESCE(sc.spoilage_multiplier, 1.0)
                         * EXP(-CAST(di.shelf_life_days AS FLOAT) / w.protection_window_days)) * di.unit_cost AS co
    ) ov

    -- Newsvendor z from the critical ratio, unless a uniform z is forced for
    -- the comparison scenarios.
    CROSS APPLY (
        SELECT CASE
            WHEN @override_z IS NOT NULL THEN @override_z
            ELSE dbo.ufn_NormInv(w.cu / NULLIF(w.cu + ov.co, 0))
        END AS z
    ) zc

    -- Safety stock in units: more variable demand (higher sigma) buys more.
    CROSS APPLY (
        SELECT COALESCE(ds.sigma_daily_demand, 0) * zc.z * SQRT(w.protection_window_days) AS safety_stock_units
    ) stock

    -- Final par, floored at 0 and capped at what can sell before it expires.
    -- The shelf-life cap is what makes this a grocery model and not a generic
    -- inventory formula: no matter what the math wants, never recommend more
    -- than velocity * shelf_life.
    CROSS APPLY (
        SELECT GREATEST(0.0, LEAST(ed.expected_demand_over_cycle + stock.safety_stock_units,
                                   ed.trailing_28d_mean_units * di.shelf_life_days)) AS par_units
    ) capped;

    SELECT @output_scenario_key AS output_scenario_key,
           @effective_date      AS effective_date,
           @@ROWCOUNT           AS rows_inserted;
END
GO
