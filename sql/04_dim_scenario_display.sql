/*
===============================================================================
Scenario Reporting Labels
===============================================================================

Adds reporting metadata to dim_scenario.

The scenario dimension contains both user-facing scenarios and diagnostic
experiments. Sort order and report visibility allow Power BI to present a
clean executive view while preserving analytical scenarios.
===============================================================================
*/

USE PlumDemo;
GO

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.dim_scenario') AND name = 'sort_order')
BEGIN
    ALTER TABLE dbo.dim_scenario ADD
        sort_order        INT NULL,
        include_in_report BIT NULL;
END
GO

UPDATE dbo.dim_scenario SET scenario_name = 'Current Ordering',          sort_order = 1, include_in_report = 1 WHERE scenario_name = 'Baseline Forward';
UPDATE dbo.dim_scenario SET scenario_name = 'Engine Recommended',        sort_order = 2, include_in_report = 1 WHERE scenario_name = 'Optimal Par';
UPDATE dbo.dim_scenario SET scenario_name = 'Industry Standard (95%)',   sort_order = 3, include_in_report = 0 WHERE scenario_name = 'Conservative Par';
UPDATE dbo.dim_scenario SET scenario_name = 'Overstock (99%)',           sort_order = 4, include_in_report = 0 WHERE scenario_name = 'Aggressive Par';
UPDATE dbo.dim_scenario SET scenario_name = 'Current Ordering (frozen)', sort_order = 9, include_in_report = 0 WHERE scenario_name = 'Baseline Frozen';
UPDATE dbo.dim_scenario SET sort_order = 0, include_in_report = 0 WHERE scenario_name = 'Historical';
GO

PRINT '--- dim_scenario after rename ---';
SELECT scenario_key, scenario_name, sort_order, include_in_report FROM dbo.dim_scenario ORDER BY sort_order;
GO

PRINT '--- validation: every scenario has sort_order/include_in_report set ---';
SELECT CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result, COUNT(*) AS unset_rows
FROM dbo.dim_scenario WHERE sort_order IS NULL OR include_in_report IS NULL;
GO
