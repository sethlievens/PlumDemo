-- v_eval_topk_by_store_day
-- Ranks each run's detections within (store, detected_date), ordered by
-- est_lost_revenue DESC (ties broken by confidence_score DESC) -- this is
-- the order a store manager would actually see them in on a morning
-- alert list. K-agnostic by design: rank_in_day is computed once for every
-- detection, and the caller filters WHERE rank_in_day <= @k for whichever K
-- it's scoring (10, 25, ...) rather than this view baking in a specific K
-- (used for precision@K analysis of the detector).
--
-- Idempotent: DROP VIEW IF EXISTS + CREATE.

DROP VIEW IF EXISTS dbo.v_eval_topk_by_store_day;
GO

CREATE VIEW dbo.v_eval_topk_by_store_day AS
SELECT
    m.run_id,
    m.store_key,
    m.detected_date,
    m.detection_key,
    m.family,
    m.matched_event_id,
    dl.est_lost_revenue,
    m.confidence_score,
    ROW_NUMBER() OVER (
        PARTITION BY m.run_id, m.store_key, m.detected_date
        ORDER BY dl.est_lost_revenue DESC, m.confidence_score DESC
    ) AS rank_in_day
FROM dbo.v_eval_matches m
JOIN dbo.detection_log dl ON dl.detection_key = m.detection_key;
GO
