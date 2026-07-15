/*
===============================================================================
Extend Date Dimension
===============================================================================

Extends dim_date to cover the full analysis and forward simulation period.

The original date dimension ended before the fact tables, causing unmatched
date joins in reporting layers. This script adds missing dates through the end
of the simulation buffer period.

Idempotent: only inserts dates after the current maximum date.
===============================================================================
*/

USE PlumDemo;
GO

SET LANGUAGE us_english;  -- lock DATENAME(WEEKDAY, ...) to English regardless of session default
GO

DECLARE @start_date DATE = (SELECT DATEADD(DAY, 1, MAX([date])) FROM dbo.dim_date);
DECLARE @end_date   DATE = '2017-10-31';

IF @start_date <= @end_date
BEGIN
    ;WITH date_spine AS (
        SELECT @start_date AS [date]
        UNION ALL
        SELECT DATEADD(DAY, 1, [date])
        FROM date_spine
        WHERE [date] < @end_date
    )
    INSERT INTO dbo.dim_date (date_key, [date], day_of_week, week_of_year, [month], is_holiday, holiday_name, is_weekend)
    SELECT
        CONVERT(INT, FORMAT([date], 'yyyyMMdd'))                          AS date_key,
        [date],
        DATENAME(WEEKDAY, [date])                                        AS day_of_week,
        DATEPART(ISO_WEEK, [date])                                       AS week_of_year,
        MONTH([date])                                                     AS [month],
        0                                                                 AS is_holiday,
        NULL                                                              AS holiday_name,
        CASE WHEN DATENAME(WEEKDAY, [date]) IN ('Saturday', 'Sunday') THEN 1 ELSE 0 END AS is_weekend
    FROM date_spine
    OPTION (MAXRECURSION 100);
END
GO

-- ============================================================
-- Validation gate
-- ============================================================

PRINT '--- dim_date coverage ---';
SELECT
    CASE WHEN MAX(date_key) >= 20171031 THEN 'PASS' ELSE 'FAIL' END AS result,
    MIN(date_key) AS min_date_key,
    MAX(date_key) AS max_date_key,
    COUNT(*) AS row_count
FROM dbo.dim_date;
GO

PRINT '--- referential integrity: fact x dim orphan counts (0 required) ---';

SELECT check_name, orphan_count, CASE WHEN orphan_count = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM (
    SELECT 'fact_sales x dim_date'            AS check_name, COUNT(*) AS orphan_count FROM dbo.fact_sales f LEFT JOIN dbo.dim_date d ON f.date_key = d.date_key WHERE d.date_key IS NULL
    UNION ALL SELECT 'fact_sales x dim_store',            COUNT(*) FROM dbo.fact_sales f LEFT JOIN dbo.dim_store s ON f.store_key = s.store_key WHERE s.store_key IS NULL
    UNION ALL SELECT 'fact_sales x dim_item',             COUNT(*) FROM dbo.fact_sales f LEFT JOIN dbo.dim_item i ON f.item_key = i.item_key WHERE i.item_key IS NULL
    UNION ALL SELECT 'fact_sales x dim_scenario',         COUNT(*) FROM dbo.fact_sales f LEFT JOIN dbo.dim_scenario sc ON f.scenario_key = sc.scenario_key WHERE sc.scenario_key IS NULL

    UNION ALL SELECT 'fact_lost_sales x dim_date',        COUNT(*) FROM dbo.fact_lost_sales f LEFT JOIN dbo.dim_date d ON f.date_key = d.date_key WHERE d.date_key IS NULL
    UNION ALL SELECT 'fact_lost_sales x dim_store',       COUNT(*) FROM dbo.fact_lost_sales f LEFT JOIN dbo.dim_store s ON f.store_key = s.store_key WHERE s.store_key IS NULL
    UNION ALL SELECT 'fact_lost_sales x dim_item',        COUNT(*) FROM dbo.fact_lost_sales f LEFT JOIN dbo.dim_item i ON f.item_key = i.item_key WHERE i.item_key IS NULL
    UNION ALL SELECT 'fact_lost_sales x dim_scenario',    COUNT(*) FROM dbo.fact_lost_sales f LEFT JOIN dbo.dim_scenario sc ON f.scenario_key = sc.scenario_key WHERE sc.scenario_key IS NULL

    UNION ALL SELECT 'fact_waste x dim_date',             COUNT(*) FROM dbo.fact_waste f LEFT JOIN dbo.dim_date d ON f.date_key = d.date_key WHERE d.date_key IS NULL
    UNION ALL SELECT 'fact_waste x dim_store',            COUNT(*) FROM dbo.fact_waste f LEFT JOIN dbo.dim_store s ON f.store_key = s.store_key WHERE s.store_key IS NULL
    UNION ALL SELECT 'fact_waste x dim_item',             COUNT(*) FROM dbo.fact_waste f LEFT JOIN dbo.dim_item i ON f.item_key = i.item_key WHERE i.item_key IS NULL
    UNION ALL SELECT 'fact_waste x dim_scenario',         COUNT(*) FROM dbo.fact_waste f LEFT JOIN dbo.dim_scenario sc ON f.scenario_key = sc.scenario_key WHERE sc.scenario_key IS NULL

    UNION ALL SELECT 'fact_inventory_snap x dim_date',    COUNT(*) FROM dbo.fact_inventory_snap f LEFT JOIN dbo.dim_date d ON f.date_key = d.date_key WHERE d.date_key IS NULL
    UNION ALL SELECT 'fact_inventory_snap x dim_store',   COUNT(*) FROM dbo.fact_inventory_snap f LEFT JOIN dbo.dim_store s ON f.store_key = s.store_key WHERE s.store_key IS NULL
    UNION ALL SELECT 'fact_inventory_snap x dim_item',    COUNT(*) FROM dbo.fact_inventory_snap f LEFT JOIN dbo.dim_item i ON f.item_key = i.item_key WHERE i.item_key IS NULL
    UNION ALL SELECT 'fact_inventory_snap x dim_scenario',COUNT(*) FROM dbo.fact_inventory_snap f LEFT JOIN dbo.dim_scenario sc ON f.scenario_key = sc.scenario_key WHERE sc.scenario_key IS NULL

    UNION ALL SELECT 'fact_receipts x dim_date',          COUNT(*) FROM dbo.fact_receipts f LEFT JOIN dbo.dim_date d ON f.date_key = d.date_key WHERE d.date_key IS NULL
    UNION ALL SELECT 'fact_receipts x dim_store',         COUNT(*) FROM dbo.fact_receipts f LEFT JOIN dbo.dim_store s ON f.store_key = s.store_key WHERE s.store_key IS NULL
    UNION ALL SELECT 'fact_receipts x dim_item',          COUNT(*) FROM dbo.fact_receipts f LEFT JOIN dbo.dim_item i ON f.item_key = i.item_key WHERE i.item_key IS NULL
    UNION ALL SELECT 'fact_receipts x dim_scenario',      COUNT(*) FROM dbo.fact_receipts f LEFT JOIN dbo.dim_scenario sc ON f.scenario_key = sc.scenario_key WHERE sc.scenario_key IS NULL
) checks
ORDER BY check_name;
GO
