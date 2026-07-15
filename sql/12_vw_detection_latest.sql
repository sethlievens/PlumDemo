/*
===============================================================================
Latest Phantom Detection Run View
===============================================================================

Creates the reporting view used by Power BI.

detection_log maintains historical detector runs for audit and evaluation.
This view exposes only the most recent run so dashboard metrics do not
double-count historical detections.

The underlying history remains available in detection_log.
===============================================================================
*/

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
