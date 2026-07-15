/*
===============================================================================
Round 2 Performance Tuning
velocity_daily
===============================================================================

This table intentionally remains on its original indexing strategy.

During tuning, several indexing approaches were evaluated, including a
clustered columnstore index and an additional nonclustered covering index.
Neither improved overall workload performance because velocity_daily is fully
rebuilt on every execution of usp_CalculateVelocity.

The detailed benchmark results and design decisions are documented in
docs/TUNING.md.

This script simply restores the baseline indexing if any experimental indexes
are still present, making it safe to rerun.
===============================================================================
*/

USE PlumDemo;
GO

SET QUOTED_IDENTIFIER ON;
GO

/*------------------------------------------------------------------------------
    Remove experimental nonclustered index (if present)
------------------------------------------------------------------------------*/

DROP INDEX IF EXISTS IX_velocity_daily_scenario_store_item_date
ON dbo.velocity_daily;
GO

/*------------------------------------------------------------------------------
    Restore the original clustered primary key if the table was converted to
    a clustered columnstore during testing.
------------------------------------------------------------------------------*/

IF EXISTS
(
    SELECT 1
    FROM sys.indexes
    WHERE object_id = OBJECT_ID('dbo.velocity_daily')
      AND type_desc = 'CLUSTERED COLUMNSTORE'
)
BEGIN

    PRINT 'Restoring velocity_daily to its baseline rowstore structure...';

    DROP INDEX CCI_velocity_daily
    ON dbo.velocity_daily;

    DECLARE
        @pk_name sysname,
        @pk_name_q sysname;

    SELECT @pk_name = kc.name
    FROM sys.key_constraints kc
    WHERE kc.parent_object_id = OBJECT_ID('dbo.velocity_daily')
      AND kc.type = 'PK';

    IF @pk_name IS NOT NULL
    BEGIN
        SET @pk_name_q = QUOTENAME(@pk_name);

        EXEC
        (
            'ALTER TABLE dbo.velocity_daily DROP CONSTRAINT '
            + @pk_name_q
        );
    END

    ALTER TABLE dbo.velocity_daily
        ADD CONSTRAINT PK_velocity_daily
        PRIMARY KEY CLUSTERED (velocity_key);

END
GO

PRINT 'velocity_daily restored to the baseline indexing strategy.';
GO
