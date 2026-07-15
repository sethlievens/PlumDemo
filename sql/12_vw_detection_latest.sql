-- 12_vw_detection_latest.sql
-- Single-run source for the Power BI phantom-detection report.
--
-- detection_log is append-only BY DESIGN (see usp_DetectPhantomInventory.sql):
-- every detector run gets its own run_id and nothing is ever cleared, so the
-- full history is the audit trail for the threshold sweeps the evaluation work
-- depends on. The report, though, wants exactly ONE run -- reading every run
-- at once double-counts detections and makes every tile wrong. This view hands
-- Power BI just the most recent run_id; the base table keeps its history.
--
-- Point the report at dbo.vw_detection_latest instead of dbo.detection_log.
-- store_key / item_key stay raw so the report can relate the view to
-- dim_store / dim_item the same way it relates the fact tables.
--
-- Idempotent: DROP VIEW IF EXISTS + CREATE.

USE PlumDemo;
GO

DROP VIEW IF EXISTS dbo.vw_detection_latest;
GO

CREATE VIEW dbo.vw_detection_latest AS
SELECT
    detection_key,
    run_id,
    run_ts,
    store_key,
    item_key,
    detected_date,
    days_dead,
    expected_units,
    confidence_score,
    est_lost_units,
    est_lost_revenue
FROM dbo.detection_log
WHERE run_id = (SELECT MAX(run_id) FROM dbo.detection_log);
GO
