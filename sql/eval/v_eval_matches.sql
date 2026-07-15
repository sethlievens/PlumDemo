-- v_eval_matches
-- Core join for scoring usp_DetectPhantomInventory against ground truth.
-- One row per detection_log row (grain confirmed one-row-per-EVENT, not
-- per-day -- see python/09_evaluate.py's grain check), LEFT JOINed to
-- phantom_events on store/item + detected_date falling inside the planted
-- event's [start_date, end_date] window. matched_event_id IS NULL means
-- false positive; non-NULL means true positive.
--
-- Safe from fan-out: no store-item in phantom_events has two overlapping
-- event windows (verified directly -- 0 overlapping pairs), so this LEFT
-- JOIN can never match more than one phantom_events row per detection.
--
-- Idempotent: DROP VIEW IF EXISTS + CREATE.

DROP VIEW IF EXISTS dbo.v_eval_matches;
GO

CREATE VIEW dbo.v_eval_matches AS
SELECT
    dl.run_id,
    dl.detection_key,
    dl.store_key,
    dl.item_key,
    di.family,
    dl.detected_date,
    dl.days_dead,
    dl.expected_units,
    dl.confidence_score,
    pe.event_id AS matched_event_id
FROM dbo.detection_log dl
JOIN dbo.dim_item di ON di.item_key = dl.item_key
LEFT JOIN dbo.phantom_events pe
    ON pe.store_key = dl.store_key AND pe.item_key = dl.item_key
   AND dl.detected_date BETWEEN pe.start_date AND pe.end_date;
GO
