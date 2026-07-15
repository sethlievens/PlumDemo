/*
===============================================================================
Create Dollar Cost Frontier Table
===============================================================================

Creates the table used for the forward simulation ordering-cost analysis.

This frontier evaluates the tradeoff between:
    • Spoilage cost
    • Lost sales cost
    • Inventory carrying cost

Unlike frontier_curve, which evaluates operational inventory metrics, this
table represents dollar-based optimization results.

Idempotent: safe to rerun.
===============================================================================
*/

USE PlumDemo;
GO

DROP TABLE IF EXISTS dbo.frontier_deli;  -- superseded DELI-only predecessor
DROP TABLE IF EXISTS dbo.frontier_dollar;
GO

CREATE TABLE dbo.frontier_dollar (
    frontier_dollar_key   INT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    family                VARCHAR(50)   NOT NULL,
    z                     DECIMAL(6,2)  NULL,      -- the swept z (can be negative); NULL for the engine's own derived point
    days_of_safety_stock  DECIMAL(8,4)  NOT NULL,  -- X axis: physical, chart-safe; negative = under-producing below expected demand
    spoilage_dollars      DECIMAL(12,2) NOT NULL,
    missed_sales_dollars  DECIMAL(12,2) NOT NULL,  -- LOST GROSS MARGIN, not lost revenue
    carrying_dollars      DECIMAL(12,2) NOT NULL,  -- 25%/yr on daily on-hand value, same convention as 12_simulate.py
    total_dollars         DECIMAL(12,2) NOT NULL,  -- spoilage + missed sales + carrying = Total Cost of Ordering
    is_engine_point       BIT           NOT NULL   -- 1 = the real, already-simulated Engine Recommended point (not a swept re-simulation)
);
GO
