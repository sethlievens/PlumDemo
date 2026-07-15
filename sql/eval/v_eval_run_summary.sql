-- v_eval_run_summary
-- Per-run raw counts for precision/recall/F1 -- ratios are computed by the
-- caller (python/09_evaluate.py), not here, since recall's denominator
-- (total ground-truth events, 1958) lives in phantom_events, not this
-- table, and dividing here would hide that dependency.
--
-- COUNT(matched_event_id) relies on COUNT() ignoring NULLs: a false
-- positive has matched_event_id = NULL by construction of v_eval_matches,
-- so this counts true positives directly without a CASE expression.
--
-- Idempotent: DROP VIEW IF EXISTS + CREATE.

DROP VIEW IF EXISTS dbo.v_eval_run_summary;
GO

CREATE VIEW dbo.v_eval_run_summary AS
SELECT
    run_id,
    COUNT(*) AS total_detections,
    COUNT(matched_event_id) AS true_positive_detections,
    COUNT(*) - COUNT(matched_event_id) AS false_positive_detections
FROM dbo.v_eval_matches
GROUP BY run_id;
GO
