-- Round 2, part 2: velocity_daily -- TRIED, REVERTED, LEFT AT ROUND 1 ON PURPOSE.
--
-- docs/TUNING.md's Round 2 capture found usp_CalculateVelocity essentially flat
-- after indexing fact_sales/fact_inventory_snap -- velocity_daily (DELETE + full
-- rebuild INSERT of the whole scenario, ~1.8M rows, EVERY run) dominates that
-- proc's I/O and was out of the original four-index scope. Two fixes were tried
-- against it; both made things worse, for two different, both-instructive reasons.
--
-- ATTEMPT 1: clustered columnstore index, same reasoning as fact_sales. WRONG
-- for this table -- made usp_CalculateVelocity catastrophically worse (45.9s ->
-- 81-147s across repeated runs, logical reads 1.09M -> up to 278M and climbing
-- with every run). Root cause, confirmed via
-- sys.dm_db_column_store_row_group_physical_stats: a CCI's DELETE is a soft
-- delete (rows marked in a delete bitmap, not physically removed until a
-- REORGANIZE); velocity_daily's DELETE-all-then-INSERT-all-every-run pattern
-- accumulates ghost rows on every single execution with nothing to purge them.
-- After repeated runs during this project's own testing, the physical rowgroups
-- held 4,426,970 rows for a table whose real (COUNT(*)) size is 1,825,182 --
-- 2,601,788 dead rows still being scanned on every read. Columnstore is for
-- bulk-loaded, rarely-deleted analytical tables (fact_sales: loaded once, never
-- touched again); a full-rebuild-per-run write pattern is the textbook case
-- columnstore explicitly warns against without a maintenance job to reorganize
-- it, which this demo has no scheduler for.
--
-- ATTEMPT 2: revert the clustered structure to rowstore (no ghosting), keep a
-- covering NONCLUSTERED index for the read-heavy path (usp_DetectPhantomInventory
-- and usp_RecommendParLevels both read this table repeatedly). Confirmed the
-- clustered rowstore structure itself was clean this time (0.01% fragmentation,
-- record_count exactly matched row count -- no residual ghosting from attempt 1).
-- Still net negative: usp_CalculateVelocity is this table's only WRITER, and
-- maintaining a second index during every DELETE+INSERT of 1.8M rows cost more
-- than the extra NC index saved its two READERS (45.9s/1.09M reads -> 72.3s/7.8M
-- reads). The two readers' actual win came almost entirely from the OPTION
-- (HASH JOIN) / inline hint fixes to their own procs (see
-- usp_DetectPhantomInventory.sql, usp_RecommendParLevels.sql), not from this
-- index -- confirmed by re-testing both WITHOUT it (still clear wins).
--
-- DECISION: leave velocity_daily at Round 1 (identity-only clustered PK, no
-- other indexes). Its full-rebuild-every-run write pattern has a real
-- architectural floor that no index shape gets under -- fixing it for real
-- would mean changing the rebuild strategy itself (e.g. build into a staging
-- table and swap, or compute incrementally instead of recomputing the whole
-- scenario every run), which is a code change to usp_CalculateVelocity.sql,
-- not an indexing change, and out of scope for this round.
--
-- This script is intentionally a no-op today (asserts/reverts to Round 1 if
-- either prior attempt's objects are still present) -- kept so the two failed
-- attempts are reproducible and the reasoning doesn't silently disappear from
-- the repo history.

USE PlumDemo;
GO
SET QUOTED_IDENTIFIER ON;
GO

DROP INDEX IF EXISTS IX_velocity_daily_scenario_store_item_date ON dbo.velocity_daily;
GO

IF EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('dbo.velocity_daily') AND type_desc = 'CLUSTERED COLUMNSTORE'
)
BEGIN
    PRINT 'velocity_daily: reverting clustered columnstore back to rowstore (ghost-row anti-pattern -- see header)';
    DROP INDEX CCI_velocity_daily ON dbo.velocity_daily;

    DECLARE @pk_name sysname, @pk_name_q sysname;
    SELECT @pk_name = kc.name
    FROM sys.key_constraints kc
    WHERE kc.parent_object_id = OBJECT_ID('dbo.velocity_daily') AND kc.type = 'PK';
    IF @pk_name IS NOT NULL
    BEGIN
        SET @pk_name_q = QUOTENAME(@pk_name);
        EXEC('ALTER TABLE dbo.velocity_daily DROP CONSTRAINT ' + @pk_name_q);
    END

    ALTER TABLE dbo.velocity_daily
        ADD CONSTRAINT PK_velocity_daily PRIMARY KEY CLUSTERED (velocity_key);
END
GO

PRINT 'velocity_daily left at Round 1 indexing (deliberately -- see this file''s header).';
GO
