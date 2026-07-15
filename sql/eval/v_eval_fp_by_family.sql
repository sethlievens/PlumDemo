-- v_eval_fp_by_family
-- Same idea as v_eval_run_summary, broken out per family within each run --
-- this is what python/09_evaluate.py reads to answer "which departments is
-- the detector worst on."
--
-- Idempotent: DROP VIEW IF EXISTS + CREATE.

DROP VIEW IF EXISTS dbo.v_eval_fp_by_family;
GO

CREATE VIEW dbo.v_eval_fp_by_family AS
SELECT
    run_id,
    family,
    COUNT(*) AS total_detections,
    COUNT(matched_event_id) AS true_positives,
    COUNT(*) - COUNT(matched_event_id) AS false_positives
FROM dbo.v_eval_matches
GROUP BY run_id, family;
GO
