/*
===============================================================================
Round 2 Performance Tuning
===============================================================================

Applies the production indexing strategy after capturing baseline performance.

Changes:
    • Convert fact_sales to a clustered columnstore index
    • Add a covering index for store/item/date lookups
    • Add a filtered index for promotional sales
    • Recluster fact_inventory_snap on its primary access pattern

The script is idempotent and can be safely rerun.
===============================================================================
*/

USE PlumDemo;
GO

SET QUOTED_IDENTIFIER ON;
GO

/*==============================================================================
    fact_sales
    Convert to clustered columnstore and recreate supporting indexes
==============================================================================*/

IF NOT EXISTS
(
    SELECT 1
    FROM sys.indexes
    WHERE object_id = OBJECT_ID('dbo.fact_sales')
      AND type_desc = 'CLUSTERED COLUMNSTORE'
)
BEGIN

    PRINT 'Converting fact_sales to clustered columnstore...';

    DROP INDEX IF EXISTS IX_fact_sales_store_item_date
        ON dbo.fact_sales;

    DROP INDEX IF EXISTS IX_fact_sales_promo
        ON dbo.fact_sales;

    DECLARE
        @pk_name sysname,
        @pk_name_q sysname;

    SELECT @pk_name = kc.name
    FROM sys.key_constraints kc
    WHERE kc.parent_object_id = OBJECT_ID('dbo.fact_sales')
      AND kc.type = 'PK';

    IF @pk_name IS NOT NULL
    BEGIN
        SET @pk_name_q = QUOTENAME(@pk_name);

        EXEC
        (
            'ALTER TABLE dbo.fact_sales DROP CONSTRAINT '
            + @pk_name_q
        );
    END

    CREATE CLUSTERED COLUMNSTORE INDEX CCI_fact_sales
        ON dbo.fact_sales;

    -- Keep the surrogate key as a nonclustered primary key.
    ALTER TABLE dbo.fact_sales
        ADD CONSTRAINT PK_fact_sales
        PRIMARY KEY NONCLUSTERED (sales_key);

END
GO

/*------------------------------------------------------------------------------
    Covering index for store/item/date lookups
------------------------------------------------------------------------------*/

DROP INDEX IF EXISTS IX_fact_sales_store_item_date
ON dbo.fact_sales;

CREATE NONCLUSTERED INDEX IX_fact_sales_store_item_date
ON dbo.fact_sales
(
    store_key,
    item_key,
    date_key
)
INCLUDE
(
    units_sold
);
GO

/*------------------------------------------------------------------------------
    Filtered index for promotional sales
------------------------------------------------------------------------------*/

DROP INDEX IF EXISTS IX_fact_sales_promo
ON dbo.fact_sales;

CREATE NONCLUSTERED INDEX IX_fact_sales_promo
ON dbo.fact_sales
(
    store_key,
    item_key,
    date_key
)
INCLUDE
(
    units_sold
)
WHERE on_promo_flag = 1;
GO

/*==============================================================================
    fact_inventory_snap
    Recluster on the primary reporting key
==============================================================================*/

IF NOT EXISTS
(
    SELECT 1
    FROM sys.indexes i
    WHERE i.object_id = OBJECT_ID('dbo.fact_inventory_snap')
      AND i.type_desc = 'CLUSTERED'
      AND EXISTS
      (
          SELECT 1
          FROM sys.index_columns ic
          JOIN sys.columns c
              ON c.object_id = ic.object_id
             AND c.column_id = ic.column_id
          WHERE ic.object_id = i.object_id
            AND ic.index_id = i.index_id
            AND ic.key_ordinal = 1
            AND c.name = 'date_key'
      )
)
BEGIN

    PRINT 'Reclustering fact_inventory_snap...';

    DECLARE
        @pk_name2 sysname,
        @pk_name2_q sysname;

    SELECT @pk_name2 = kc.name
    FROM sys.key_constraints kc
    WHERE kc.parent_object_id = OBJECT_ID('dbo.fact_inventory_snap')
      AND kc.type = 'PK';

    IF @pk_name2 IS NOT NULL
    BEGIN
        SET @pk_name2_q = QUOTENAME(@pk_name2);

        EXEC
        (
            'ALTER TABLE dbo.fact_inventory_snap DROP CONSTRAINT '
            + @pk_name2_q
        );
    END

    ALTER TABLE dbo.fact_inventory_snap
        ADD CONSTRAINT PK_fact_inventory_snap
        PRIMARY KEY NONCLUSTERED (inv_snap_key);

    CREATE CLUSTERED INDEX CIX_fact_inventory_snap
        ON dbo.fact_inventory_snap
        (
            date_key,
            store_key,
            item_key
        );

END
GO

/*------------------------------------------------------------------------------
    Refresh statistics after structural changes
------------------------------------------------------------------------------*/

UPDATE STATISTICS dbo.fact_sales
WITH FULLSCAN;

UPDATE STATISTICS dbo.fact_inventory_snap
WITH FULLSCAN;
GO

PRINT 'Round 2 indexing complete.';
GO
