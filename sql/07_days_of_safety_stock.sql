-- 07_days_of_safety_stock.sql
-- derived_service_level is a CYCLE service level (P(no stockout within one
-- replenishment cycle)), not an in-stock rate -- it does not belong on a
-- slide next to a metric like realized in-stock rate, it reads as "we plan
-- to be out of stock 43% of the time" for DELI. days_of_safety_stock
-- (safety_stock_units / daily velocity) is the physical, chart-safe number
-- instead: how many days of average demand the safety-stock portion alone
-- covers.
--
-- Idempotent: ALTER TABLE guarded by column-existence check; proc calls are
-- idempotent by construction (each DELETEs its own @output_scenario_key's
-- rows first).

USE PlumDemo;
GO

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.par_levels') AND name = 'days_of_safety_stock')
    ALTER TABLE dbo.par_levels ADD days_of_safety_stock DECIMAL(8,4) NULL;
GO

EXEC dbo.usp_RecommendParLevels @source_scenario_key = 1, @output_scenario_key = 3, @override_z = NULL;
EXEC dbo.usp_RecommendParLevels @source_scenario_key = 1, @output_scenario_key = 4, @override_z = 1.65;
EXEC dbo.usp_RecommendParLevels @source_scenario_key = 1, @output_scenario_key = 5, @override_z = 2.33;
GO

PRINT '--- validation: days_of_safety_stock populated (0 NULLs) ---';
SELECT
    ds.scenario_name,
    COUNT(*) AS row_count,
    SUM(CASE WHEN pl.days_of_safety_stock IS NULL THEN 1 ELSE 0 END) AS null_count,
    CASE WHEN SUM(CASE WHEN pl.days_of_safety_stock IS NULL THEN 1 ELSE 0 END) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM dbo.par_levels pl
JOIN dbo.dim_scenario ds ON ds.scenario_key = pl.scenario_key
WHERE pl.scenario_key IN (3, 4, 5)
GROUP BY ds.scenario_name
ORDER BY ds.scenario_name;
GO

PRINT '--- days_of_safety_stock by family, Engine Recommended ---';
SELECT
    di.family,
    COUNT(*) AS items,
    CAST(AVG(pl.days_of_safety_stock) AS DECIMAL(6,2)) AS avg_days_of_safety_stock
FROM dbo.par_levels pl
JOIN dbo.dim_item di ON di.item_key = pl.item_key
WHERE pl.scenario_key = 3
GROUP BY di.family
ORDER BY avg_days_of_safety_stock DESC;
GO
