-- 06_create_frontier_curve.sql
-- artifacts/frontier.parquet (the backfill-calibration buffer sweep behind
-- docs/FINDINGS.md's waste%/top-up% analysis) only lives on disk. Creates
-- the table Power BI reads it from over the same SQL connection as
-- everything else. Loaded by python/13_load_frontier.py.
--
-- Idempotent: DROP TABLE IF EXISTS + CREATE, like every table in
-- 01_schema.sql (this DDL is duplicated there for a from-scratch rebuild;
-- this standalone file is so it can be created now without dropping and
-- reloading every other table in the database).

USE PlumDemo;
GO

DROP TABLE IF EXISTS dbo.frontier_curve;
GO

CREATE TABLE dbo.frontier_curve (
    frontier_key    INT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    family          VARCHAR(50)   NOT NULL,
    buffer          DECIMAL(6,2)  NOT NULL,
    waste_pct       DECIMAL(8,4)  NOT NULL,
    days_of_supply  DECIMAL(8,4)  NOT NULL,
    emergency_rate  DECIMAL(8,4)  NOT NULL,
    days_of_stock   DECIMAL(8,4)  NOT NULL
);
GO
