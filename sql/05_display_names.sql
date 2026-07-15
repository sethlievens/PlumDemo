/*
===============================================================================
Add Display Labels
===============================================================================

Adds presentation-friendly labels for Power BI reporting.

Original database attributes are preserved for joins and calculations. These
columns exist only to improve chart labels and report readability.
===============================================================================
*/

USE PlumDemo;
GO

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.dim_item') AND name = 'display_family')
    ALTER TABLE dbo.dim_item ADD display_family VARCHAR(50) NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.dim_store') AND name = 'display_name')
    ALTER TABLE dbo.dim_store ADD display_name VARCHAR(50) NULL;
GO

UPDATE dbo.dim_item SET display_family = CASE family
    WHEN 'GROCERY'      THEN 'Grocery (dry goods)'
    WHEN 'BREAD/BAKERY' THEN 'Bakery'
    WHEN 'DELI'         THEN 'Deli & Prepared'
    WHEN 'PRODUCE'      THEN 'Produce'
    WHEN 'DAIRY'        THEN 'Dairy'
    WHEN 'BEVERAGES'    THEN 'Beverages'
    ELSE family
END;
GO

-- Short, realistic store names -- each is a real neighborhood/mall in that
-- store's actual city (two-store cities get neighborhood names, single-store
-- cities get the city). Chart-axis-friendly; store_type stays its own column.
UPDATE dbo.dim_store SET display_name = CASE store_nbr
    WHEN 3  THEN 'El Recreo'      -- Quito (type D)
    WHEN 44 THEN 'Quicentro'      -- Quito (type A)
    WHEN 28 THEN 'Alborada'       -- Guayaquil (type E)
    WHEN 34 THEN 'Mall del Sol'   -- Guayaquil (type B)
    WHEN 13 THEN 'Latacunga'      -- Latacunga (type C)
    WHEN 41 THEN 'Machala'        -- Machala (type D)
    ELSE 'Store ' + CAST(store_nbr AS VARCHAR)
END;
GO

PRINT '--- dim_item.display_family ---';
SELECT DISTINCT family, display_family FROM dbo.dim_item ORDER BY family;
GO

PRINT '--- dim_store.display_name ---';
SELECT store_key, store_nbr, store_type, display_name FROM dbo.dim_store ORDER BY store_key;
GO

PRINT '--- validation: no unmapped rows ---';
SELECT
    CASE WHEN (SELECT COUNT(*) FROM dbo.dim_item WHERE display_family IS NULL) = 0
      AND (SELECT COUNT(*) FROM dbo.dim_store WHERE display_name IS NULL) = 0
    THEN 'PASS' ELSE 'FAIL' END AS result;
GO
