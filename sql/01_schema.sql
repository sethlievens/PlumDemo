-- PlumDemo schema, per docs/SCHEMA.md.
-- Idempotent: every object is DROP IF EXISTS'd before create, safe to re-run.
--
-- ROUND 1 INDEXING ONLY -- DELIBERATE.
-- Every table gets nothing but a clustered PK on its surrogate identity
-- column. No nonclustered indexes, no foreign keys, no filtered indexes.
-- This is intentional: we want a genuinely bad execution plan (scans, no
-- seek paths, no columnstore on the big fact tables) to capture as the
-- "before" picture. Round 2 adds the real indexing (columnstore on
-- fact_sales, covering NC on store/item/date, filtered index on promo) and
-- we capture before/after plans as artifacts. Don't "fix" this file to be
-- fast -- that's the point of round 2, not round 1.

IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name = 'PlumDemo')
BEGIN
    PRINT 'Creating database PlumDemo';
    EXEC('CREATE DATABASE PlumDemo');
END
GO

USE PlumDemo;
GO

-- Drop in reverse-dependency order (facts/config before dims) so re-running
-- this script never trips over FK-less-but-still-ordered drops.
DROP TABLE IF EXISTS dbo.frontier_dollar;
DROP TABLE IF EXISTS dbo.frontier_curve;
DROP TABLE IF EXISTS dbo.detection_log;
DROP TABLE IF EXISTS dbo.phantom_events;
DROP TABLE IF EXISTS dbo.par_levels;
DROP TABLE IF EXISTS dbo.fact_lost_sales;
DROP TABLE IF EXISTS dbo.fact_waste;
DROP TABLE IF EXISTS dbo.fact_receipts;
DROP TABLE IF EXISTS dbo.fact_inventory_snap;
DROP TABLE IF EXISTS dbo.fact_sales;
DROP TABLE IF EXISTS dbo.dim_spoilage_calibration;
DROP TABLE IF EXISTS dbo.dim_scenario;
DROP TABLE IF EXISTS dbo.dim_vendor;
DROP TABLE IF EXISTS dbo.dim_date;
DROP TABLE IF EXISTS dbo.dim_item;
DROP TABLE IF EXISTS dbo.dim_department;
DROP TABLE IF EXISTS dbo.dim_store;
GO

-- ============================================================
-- Dimensions
-- ============================================================

CREATE TABLE dbo.dim_store (
    store_key       INT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    store_nbr       INT NOT NULL,
    city            VARCHAR(50)  NULL,
    state           VARCHAR(50)  NULL,
    store_type      CHAR(1)      NULL,
    cluster_id      INT          NULL,
    peer_group_id   INT          NULL,
    display_name    VARCHAR(50)  NULL  -- e.g. "Store 3 (Type D)", chart-axis-friendly
    -- peer_group_id derived from store_type, not cluster_id: our 6-store demo
    -- subset has a distinct cluster per store (no cluster overlap at all), so
    -- clustering on cluster_id would make every store its own singleton peer
    -- group and defeat cross-store phantom detection entirely. store_type
    -- does overlap in this subset (two type-D stores), giving at least one
    -- real peer comparison. Revisit if the store subset changes.
);

CREATE TABLE dbo.dim_department (
    dept_key    INT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    dept_name   VARCHAR(50) NOT NULL
);

CREATE TABLE dbo.dim_item (
    item_key            INT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    item_nbr            INT NOT NULL,
    family              VARCHAR(50)     NULL,
    class               INT             NULL,
    is_perishable       BIT             NULL,
    shelf_life_days     INT             NULL,
    unit_cost           DECIMAL(10,2)   NULL,
    retail_price        DECIMAL(10,2)   NULL,
    target_margin_pct   DECIMAL(6,4)    NULL,
    case_pack_qty       INT             NULL,
    cadence_days        INT             NULL,
    dept_key            INT             NULL,
    display_family      VARCHAR(50)     NULL  -- e.g. "Deli & Prepared", chart-legend-friendly
);

CREATE TABLE dbo.dim_date (
    date_key        INT PRIMARY KEY CLUSTERED,  -- yyyymmdd smart key, not an identity -- the natural, idiomatic date-dim key
    [date]          DATE NOT NULL,
    day_of_week     VARCHAR(10) NOT NULL,
    week_of_year    INT NOT NULL,
    [month]         INT NOT NULL,
    is_holiday      BIT NOT NULL,
    holiday_name    VARCHAR(100) NULL,
    is_weekend      BIT NOT NULL
);

CREATE TABLE dbo.dim_vendor (
    vendor_key          INT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    vendor_name         VARCHAR(100) NOT NULL,
    lead_time_days      INT NULL,
    delivery_days_mask  VARCHAR(7) NULL  -- 7 chars, Mon..Sun, '1' = delivery day
);

CREATE TABLE dbo.dim_scenario (
    scenario_key      INT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    scenario_name     VARCHAR(50) NOT NULL,
    description       VARCHAR(255) NULL,
    sort_order        INT NULL,  -- slicer ordering, e.g. Historical=0, Current=1, Engine=2, Industry=3, Overstock=4, frozen=9
    include_in_report BIT NULL   -- 1 = shown in the Power BI two-button slicer (Current Ordering, Engine Recommended); 0 = diagnostic/strawman, hidden
    -- 'Historical', 'Current Ordering', 'Engine Recommended', 'Industry Standard (95%)', 'Overstock (99%)', 'Current Ordering (frozen)'
);

CREATE TABLE dbo.dim_spoilage_calibration (
    family               VARCHAR(50)  NOT NULL PRIMARY KEY CLUSTERED,
    spoilage_multiplier  DECIMAL(6,3) NOT NULL,  -- v2: scales usp_RecommendParLevels' EXP(-shelf_life/window) P(spoil) term; 1.0 = v1
    source_note          VARCHAR(200) NULL
    -- Populated by sql/09_spoilage_calibration.sql from the DELI/DAIRY
    -- dollar sweeps. Families absent here default to 1.0 (v1 behavior).
);

-- ============================================================
-- Facts
-- ============================================================

CREATE TABLE dbo.fact_sales (
    sales_key       BIGINT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    date_key        INT NOT NULL,
    store_key       INT NOT NULL,
    item_key        INT NOT NULL,
    scenario_key    INT NOT NULL,
    units_sold      DECIMAL(10,2) NOT NULL,  -- OBSERVED: phantom-event windows are zeroed out here
    gross_revenue   DECIMAL(12,2) NOT NULL,
    cogs            DECIMAL(12,2) NOT NULL,
    on_promo_flag   BIT NOT NULL
);

CREATE TABLE dbo.fact_inventory_snap (
    inv_snap_key    BIGINT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    date_key        INT NOT NULL,
    store_key       INT NOT NULL,
    item_key        INT NOT NULL,
    scenario_key    INT NOT NULL,
    on_hand_units   DECIMAL(10,2) NOT NULL,  -- end of day, one row per store/item/day
    on_hand_value   DECIMAL(12,2) NOT NULL,
    days_of_supply  DECIMAL(8,2) NULL
);

CREATE TABLE dbo.fact_receipts (
    receipt_key         BIGINT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    date_key            INT NOT NULL,
    store_key           INT NOT NULL,
    item_key            INT NOT NULL,
    vendor_key          INT NOT NULL,
    scenario_key        INT NOT NULL,
    ordered_units       DECIMAL(10,2) NOT NULL,
    received_units      DECIMAL(10,2) NOT NULL,
    unit_cost           DECIMAL(10,2) NOT NULL,
    expiry_date         DATE NOT NULL,
    is_emergency_topup  BIT NOT NULL
);

CREATE TABLE dbo.fact_waste (
    waste_key       BIGINT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    date_key        INT NOT NULL,
    store_key       INT NOT NULL,
    item_key        INT NOT NULL,
    scenario_key    INT NOT NULL,
    units_wasted    DECIMAL(10,2) NOT NULL,
    waste_cost      DECIMAL(12,2) NOT NULL,
    reason          VARCHAR(50) NOT NULL  -- 'Expired'
);

CREATE TABLE dbo.fact_lost_sales (
    lost_sales_key      BIGINT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    date_key            INT NOT NULL,
    store_key           INT NOT NULL,
    item_key            INT NOT NULL,
    scenario_key        INT NOT NULL,
    true_demand_units   DECIMAL(10,2) NOT NULL,
    units_sold          DECIMAL(10,2) NOT NULL,
    lost_units          DECIMAL(10,2) NOT NULL,
    lost_revenue        DECIMAL(12,2) NOT NULL
    -- FORWARD SIM ONLY. Ground truth. Left empty until the forward-sim
    -- scenarios (Baseline Forward / Recommended Par / Aggressive Par) exist.
);

-- ============================================================
-- Config / ops
-- ============================================================

CREATE TABLE dbo.par_levels (
    par_level_key             INT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    store_key                 INT NOT NULL,
    item_key                  INT NOT NULL,
    scenario_key              INT NOT NULL,
    par_units                 DECIMAL(10,2) NOT NULL,
    reorder_point             DECIMAL(10,2) NOT NULL,
    safety_stock_units        DECIMAL(10,2) NOT NULL,
    effective_date            DATE NOT NULL,
    projected_spoilage_units  DECIMAL(10,2) NULL,  -- added for usp_RecommendParLevels: par_units in excess of expected cycle demand
    projected_stockout_units  DECIMAL(10,2) NULL,  -- added for usp_RecommendParLevels: expected demand the shelf-life cap left uncovered
    critical_ratio            DECIMAL(6,4)  NULL,  -- added for usp_RecommendParLevels: raw per-item newsvendor ratio Cu/(Cu+Co), regardless of @override_z
    derived_z                 DECIMAL(8,4)  NULL,  -- added for usp_RecommendParLevels: the z actually used to size safety stock (derived, floored at 0, or @override_z)
    derived_service_level     DECIMAL(6,4)  NULL,  -- added for usp_RecommendParLevels: CYCLE service level implied by derived_z. NOT an in-stock rate -- do not chart it as one, see proc header
    days_of_safety_stock      DECIMAL(8,4)  NULL   -- added for usp_RecommendParLevels: safety_stock_units / trailing_28d_mean_units -- physical, chart-safe
    -- Left empty until the par-level recommendation engine runs.
);

CREATE TABLE dbo.phantom_events (
    event_id        INT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    store_key       INT NOT NULL,
    item_key        INT NOT NULL,
    scenario_key    INT NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL
    -- GROUND TRUTH for injected empty-shelf events.
);

CREATE TABLE dbo.frontier_curve (
    frontier_key    INT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    family          VARCHAR(50)   NOT NULL,
    buffer          DECIMAL(6,2)  NOT NULL,  -- the calibration sweep's buffer multiplier (06_calibrate.py), NOT a par-level z
    waste_pct       DECIMAL(8,4)  NOT NULL,
    days_of_supply  DECIMAL(8,4)  NOT NULL,
    emergency_rate  DECIMAL(8,4)  NOT NULL,  -- emergency top-up rate, same metric FINDINGS.md calls "own top-up %"
    days_of_stock   DECIMAL(8,4)  NOT NULL   -- buffer x that family's cadence_days -- a physical quantity ("days of chicken in the case"), not an abstract multiplier
    -- Loaded from artifacts/frontier.parquet by python/13_load_frontier.py.
    -- NOTE: this is the backfill-calibration buffer sweep (waste% / days-of-
    -- supply / emergency top-up rate), a DIFFERENT "buffer" concept from
    -- par_levels' newsvendor z -- don't conflate the two on a chart.
);

CREATE TABLE dbo.frontier_dollar (
    frontier_dollar_key   INT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    family                VARCHAR(50)   NOT NULL,
    z                     DECIMAL(6,2)  NULL,      -- swept z (can be NEGATIVE = under-producing); NULL for the engine's own derived point
    days_of_safety_stock  DECIMAL(8,4)  NOT NULL,
    spoilage_dollars      DECIMAL(12,2) NOT NULL,
    missed_sales_dollars  DECIMAL(12,2) NOT NULL,  -- lost gross margin, not lost revenue -- see python/14_frontier_dollar_sweep.py
    carrying_dollars      DECIMAL(12,2) NOT NULL,
    total_dollars         DECIMAL(12,2) NOT NULL,  -- spoilage + missed sales + carrying = Total Cost of Ordering
    is_engine_point       BIT           NOT NULL
    -- Loaded from a forward-sim sweep by python/14_frontier_dollar_sweep.py --
    -- NOT the same thing as frontier_curve (the backfill calibration sweep).
);

CREATE TABLE dbo.detection_log (
    detection_key       BIGINT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    run_id              INT NOT NULL,
    run_ts              DATETIME2 NOT NULL,
    store_key           INT NOT NULL,
    item_key            INT NOT NULL,
    detected_date       DATE NOT NULL,
    days_dead           INT NOT NULL,            -- added for usp_DetectPhantomInventory
    expected_units      DECIMAL(10,2) NOT NULL,  -- added for usp_DetectPhantomInventory
    confidence_score    DECIMAL(5,4) NOT NULL,
    est_lost_units      DECIMAL(10,2) NULL,
    est_lost_revenue    DECIMAL(12,2) NULL
    -- What the detector THINKS is phantom. Score against phantom_events for
    -- precision/recall. Left empty until the detector runs.
);
GO

-- ============================================================
-- Load views -- BULK INSERT targets that expose every column EXCEPT the
-- surrogate identity PK, so SQL Server auto-assigns identities on load
-- instead of requiring a placeholder column (and a KEEPIDENTITY dance) in
-- the source CSV. BULK INSERT maps by ordinal position only -- it has no
-- column-list syntax -- so this view trick is the simplest way to skip an
-- identity column without a format file. dim_date has no identity column
-- (its key is the yyyymmdd date itself) so it loads straight into the
-- table, no view needed.
-- ============================================================

DROP VIEW IF EXISTS dbo.v_load_dim_store;
DROP VIEW IF EXISTS dbo.v_load_dim_department;
DROP VIEW IF EXISTS dbo.v_load_dim_item;
DROP VIEW IF EXISTS dbo.v_load_dim_vendor;
DROP VIEW IF EXISTS dbo.v_load_dim_scenario;
DROP VIEW IF EXISTS dbo.v_load_fact_sales;
DROP VIEW IF EXISTS dbo.v_load_fact_inventory_snap;
DROP VIEW IF EXISTS dbo.v_load_fact_receipts;
DROP VIEW IF EXISTS dbo.v_load_fact_waste;
DROP VIEW IF EXISTS dbo.v_load_fact_lost_sales;
DROP VIEW IF EXISTS dbo.v_load_par_levels;
DROP VIEW IF EXISTS dbo.v_load_phantom_events;
DROP VIEW IF EXISTS dbo.v_load_detection_log;
GO

CREATE VIEW dbo.v_load_dim_store AS
    SELECT store_nbr, city, state, store_type, cluster_id, peer_group_id, display_name FROM dbo.dim_store;
GO
CREATE VIEW dbo.v_load_dim_department AS
    SELECT dept_name FROM dbo.dim_department;
GO
CREATE VIEW dbo.v_load_dim_item AS
    SELECT item_nbr, family, class, is_perishable, shelf_life_days, unit_cost,
           retail_price, target_margin_pct, case_pack_qty, cadence_days, dept_key, display_family
    FROM dbo.dim_item;
GO
CREATE VIEW dbo.v_load_dim_vendor AS
    SELECT vendor_name, lead_time_days, delivery_days_mask FROM dbo.dim_vendor;
GO
CREATE VIEW dbo.v_load_dim_scenario AS
    SELECT scenario_name, description, sort_order, include_in_report FROM dbo.dim_scenario;
GO
CREATE VIEW dbo.v_load_fact_sales AS
    SELECT date_key, store_key, item_key, scenario_key, units_sold, gross_revenue, cogs, on_promo_flag
    FROM dbo.fact_sales;
GO
CREATE VIEW dbo.v_load_fact_inventory_snap AS
    SELECT date_key, store_key, item_key, scenario_key, on_hand_units, on_hand_value, days_of_supply
    FROM dbo.fact_inventory_snap;
GO
CREATE VIEW dbo.v_load_fact_receipts AS
    SELECT date_key, store_key, item_key, vendor_key, scenario_key, ordered_units,
           received_units, unit_cost, expiry_date, is_emergency_topup
    FROM dbo.fact_receipts;
GO
CREATE VIEW dbo.v_load_fact_waste AS
    SELECT date_key, store_key, item_key, scenario_key, units_wasted, waste_cost, reason
    FROM dbo.fact_waste;
GO
CREATE VIEW dbo.v_load_fact_lost_sales AS
    SELECT date_key, store_key, item_key, scenario_key, true_demand_units, units_sold, lost_units, lost_revenue
    FROM dbo.fact_lost_sales;
GO
CREATE VIEW dbo.v_load_par_levels AS
    SELECT store_key, item_key, scenario_key, par_units, reorder_point, safety_stock_units, effective_date,
           projected_spoilage_units, projected_stockout_units, critical_ratio, derived_z, derived_service_level,
           days_of_safety_stock
    FROM dbo.par_levels;
GO
CREATE VIEW dbo.v_load_phantom_events AS
    SELECT store_key, item_key, scenario_key, start_date, end_date FROM dbo.phantom_events;
GO
CREATE VIEW dbo.v_load_detection_log AS
    SELECT run_id, run_ts, store_key, item_key, detected_date, days_dead, expected_units,
           confidence_score, est_lost_units, est_lost_revenue
    FROM dbo.detection_log;
GO
