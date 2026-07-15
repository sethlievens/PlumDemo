-- Round 2 indexing.
-- sql/01_schema.sql deliberately shipped Round 1 (clustered PK on identity only,
-- nothing else) so a genuinely bad plan could be captured as the "before" picture
-- -- see artifacts/tuning/before/. This script is the "after": the indexing a real
-- 2M-row analytical fact table actually wants. Idempotent -- every index/constraint
-- change is guarded so re-running this script is a no-op once applied.
--
-- Four changes, in the order the user specified:
--   1. fact_sales: CLUSTERED COLUMNSTORE INDEX. This is an aggregation workload
--      (usp_CalculateVelocity/usp_DetectPhantomInventory/usp_RecommendParLevels
--      all scan the whole scenario's history and window/aggregate over it) at
--      ~1.8-2.4M rows per scenario -- exactly the shape columnstore is for:
--      10-100x compression, segment elimination, batch-mode execution.
--   2. Covering NONCLUSTERED (store_key, item_key, date_key) INCLUDE (units_sold)
--      on fact_sales -- the seek-heavy path (point lookups by store/item/date,
--      e.g. usp_DetectPhantomInventory's peer join) doesn't want a columnstore
--      segment scan; it wants a b-tree seek. CCI + a supporting rowstore NC index
--      is the standard "both workloads" pattern, not a contradiction.
--   3. fact_inventory_snap: CLUSTERED on (date_key, store_key, item_key), replacing
--      the identity-only clustered PK -- every proc joins fact_sales to
--      fact_inventory_snap on exactly (date_key, store_key, item_key, scenario_key);
--      an identity clustered key gives the optimizer nothing to seek on.
--   4. Filtered NONCLUSTERED index on fact_sales WHERE on_promo_flag = 1 -- promo
--      days are a small, frequently-isolated slice (usp_CalculateVelocity's
--      clean_sales CTE branches on this flag); a filtered index is a fraction of
--      the size of an unfiltered one over the same columns and only helps when the
--      predicate matches, at zero cost to plans that don't touch on_promo_flag.
--
-- fact_sales' surrogate PK (sales_key) survives as a NONCLUSTERED PK -- a
-- columnstore table can't also be the row-store clustered structure, but nothing
-- downstream references sales_key as a clustering key, so moving it nonclustered
-- costs nothing.

USE PlumDemo;
GO

-- Filtered indexes (and QUOTENAME, used below to safely drop the
-- auto-named PK constraints) require QUOTED_IDENTIFIER ON; sqlcmd's
-- session default is OFF, unlike SSMS.
SET QUOTED_IDENTIFIER ON;
GO

-- ============================================================
-- 1 + 2 + 4: fact_sales -> clustered columnstore + covering NC + filtered NC
-- ============================================================

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('dbo.fact_sales') AND type_desc = 'CLUSTERED COLUMNSTORE'
)
BEGIN
    PRINT 'fact_sales: converting to clustered columnstore';

    DROP INDEX IF EXISTS IX_fact_sales_store_item_date ON dbo.fact_sales;
    DROP INDEX IF EXISTS IX_fact_sales_promo ON dbo.fact_sales;

    DECLARE @pk_name sysname, @pk_name_q sysname;
    SELECT @pk_name = kc.name
    FROM sys.key_constraints kc
    WHERE kc.parent_object_id = OBJECT_ID('dbo.fact_sales') AND kc.type = 'PK';
    IF @pk_name IS NOT NULL
    BEGIN
        SET @pk_name_q = QUOTENAME(@pk_name);  -- EXEC(str + QUOTENAME(...)) inline hits a SQL Server parser quirk; assign first
        EXEC('ALTER TABLE dbo.fact_sales DROP CONSTRAINT ' + @pk_name_q);
    END

    CREATE CLUSTERED COLUMNSTORE INDEX CCI_fact_sales ON dbo.fact_sales;

    ALTER TABLE dbo.fact_sales
        ADD CONSTRAINT PK_fact_sales PRIMARY KEY NONCLUSTERED (sales_key);
END
GO

-- Covering NC for the seek-heavy detection path: key order matches every proc's
-- join predicate (store_key, item_key, date_key); units_sold riding along in
-- INCLUDE means these seeks never have to visit the columnstore rowgroups at all.
DROP INDEX IF EXISTS IX_fact_sales_store_item_date ON dbo.fact_sales;
CREATE NONCLUSTERED INDEX IX_fact_sales_store_item_date
    ON dbo.fact_sales (store_key, item_key, date_key)
    INCLUDE (units_sold);
GO

-- Filtered NC on the promo flag -- small, frequently-isolated slice.
DROP INDEX IF EXISTS IX_fact_sales_promo ON dbo.fact_sales;
CREATE NONCLUSTERED INDEX IX_fact_sales_promo
    ON dbo.fact_sales (store_key, item_key, date_key)
    INCLUDE (units_sold)
    WHERE on_promo_flag = 1;
GO

-- ============================================================
-- 3: fact_inventory_snap -> clustered on (date_key, store_key, item_key)
-- ============================================================

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes i
    WHERE i.object_id = OBJECT_ID('dbo.fact_inventory_snap')
      AND i.type_desc = 'CLUSTERED'
      AND EXISTS (
          SELECT 1 FROM sys.index_columns ic
          JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
          WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
            AND ic.key_ordinal = 1 AND c.name = 'date_key'
      )
)
BEGIN
    PRINT 'fact_inventory_snap: reclustering on (date_key, store_key, item_key)';

    DECLARE @pk_name2 sysname, @pk_name2_q sysname;
    SELECT @pk_name2 = kc.name
    FROM sys.key_constraints kc
    WHERE kc.parent_object_id = OBJECT_ID('dbo.fact_inventory_snap') AND kc.type = 'PK';
    IF @pk_name2 IS NOT NULL
    BEGIN
        SET @pk_name2_q = QUOTENAME(@pk_name2);
        EXEC('ALTER TABLE dbo.fact_inventory_snap DROP CONSTRAINT ' + @pk_name2_q);
    END

    ALTER TABLE dbo.fact_inventory_snap
        ADD CONSTRAINT PK_fact_inventory_snap PRIMARY KEY NONCLUSTERED (inv_snap_key);

    CREATE CLUSTERED INDEX CIX_fact_inventory_snap
        ON dbo.fact_inventory_snap (date_key, store_key, item_key);
END
GO

-- Fresh stats on the tables that just changed structure -- avoid an "after"
-- capture that's still plan-shopping off stale/rowstore-era statistics.
UPDATE STATISTICS dbo.fact_sales WITH FULLSCAN;
UPDATE STATISTICS dbo.fact_inventory_snap WITH FULLSCAN;
GO

PRINT 'Round 2 indexing complete.';
GO
