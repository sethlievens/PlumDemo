-- 03_add_par_level_service_metrics.sql
-- usp_RecommendParLevels computes the newsvendor critical ratio and the
-- resulting z per item, but only ever used them to size safety_stock_units
-- -- the ratio and z themselves were discarded, not persisted. That's the
-- headline number for the Power BI par-levels page (the engine deriving
-- ~99.5% service level for GROCERY vs ~85% for DELI from margin, carrying
-- cost and shelf life), and it wasn't queryable. This adds the three
-- columns to par_levels, updates the proc to persist them (see
-- sql/procs/usp_RecommendParLevels.sql and the new sql/procs/ufn_NormSDist.sql),
-- and re-runs the proc for all three par scenarios so existing rows pick up
-- the new values.
--
-- Idempotent: the ALTER TABLE is guarded by a column-existence check, safe
-- to re-run. The proc calls below are idempotent by construction (each
-- DELETEs its own @output_scenario_key's rows first).

USE PlumDemo;
GO

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.par_levels') AND name = 'critical_ratio')
BEGIN
    ALTER TABLE dbo.par_levels ADD
        critical_ratio        DECIMAL(6,4) NULL,
        derived_z             DECIMAL(8,4) NULL,
        derived_service_level DECIMAL(6,4) NULL;
END
GO

-- Re-run all three par scenarios (source = Historical) so existing rows get
-- the new columns populated. Same source/output/override combination each
-- was originally generated with -- confirmed against the live par_levels
-- data before writing this script.
EXEC dbo.usp_RecommendParLevels @source_scenario_key = 1, @output_scenario_key = 3, @override_z = NULL;  -- Optimal Par: derived z
EXEC dbo.usp_RecommendParLevels @source_scenario_key = 1, @output_scenario_key = 4, @override_z = 1.65;  -- Conservative Par: naive strawman
EXEC dbo.usp_RecommendParLevels @source_scenario_key = 1, @output_scenario_key = 5, @override_z = 2.33;  -- Aggressive Par: naive strawman
GO

-- ============================================================
-- Validation gate
-- ============================================================

PRINT '--- par_levels: new columns populated (0 NULLs required per scenario) ---';
SELECT
    ds.scenario_name,
    COUNT(*) AS row_count,
    SUM(CASE WHEN pl.critical_ratio IS NULL THEN 1 ELSE 0 END)        AS null_critical_ratio,
    SUM(CASE WHEN pl.derived_z IS NULL THEN 1 ELSE 0 END)             AS null_derived_z,
    SUM(CASE WHEN pl.derived_service_level IS NULL THEN 1 ELSE 0 END) AS null_derived_service_level,
    CASE WHEN SUM(CASE WHEN pl.critical_ratio IS NULL OR pl.derived_z IS NULL OR pl.derived_service_level IS NULL THEN 1 ELSE 0 END) = 0
         THEN 'PASS' ELSE 'FAIL' END AS result
FROM dbo.par_levels pl
JOIN dbo.dim_scenario ds ON ds.scenario_key = pl.scenario_key
GROUP BY ds.scenario_name
ORDER BY ds.scenario_name;
GO

PRINT '--- headline check: derived service level by family, Optimal Par scenario ---';
SELECT
    di.family,
    COUNT(*) AS items,
    CAST(AVG(pl.critical_ratio) * 100 AS DECIMAL(5,1))        AS avg_critical_ratio_pct,
    CAST(AVG(pl.derived_service_level) * 100 AS DECIMAL(5,1)) AS avg_derived_service_level_pct
FROM dbo.par_levels pl
JOIN dbo.dim_item di ON di.item_key = pl.item_key
WHERE pl.scenario_key = 3
GROUP BY di.family
ORDER BY avg_derived_service_level_pct DESC;
GO
