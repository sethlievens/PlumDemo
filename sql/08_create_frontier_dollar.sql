-- 08_create_frontier_dollar.sql
-- Dollar-denominated ordering-cost frontier from the forward sim, per
-- family -- see python/14_frontier_dollar_sweep.py. Supersedes the
-- DELI-only dbo.frontier_deli (dropped below): once the sweep covers more
-- than one family, a table named after one of them is a trap for whoever
-- reads the Power BI model. NOT the same thing as dbo.frontier_curve (the
-- backfill calibration sweep, units/% -- a different question).
--
-- The z sweep deliberately extends NEGATIVE for short-shelf-life families:
-- negative z = par below expected demand = deliberate under-production,
-- which is the textbook newsvendor answer whenever the critical ratio
-- drops below 0.5 (overstock cost exceeds understock cost).
--
-- Idempotent: DROP TABLE IF EXISTS + CREATE, like every table in this
-- project (DDL duplicated in 01_schema.sql for a from-scratch rebuild).

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
