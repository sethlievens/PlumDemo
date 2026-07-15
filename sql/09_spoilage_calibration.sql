-- 09_spoilage_calibration.sql
-- v2 of the par engine's overstock-cost (Co) term. Background: the DELI/
-- DAIRY dollar sweep (python/14_frontier_dollar_sweep.py) showed the true,
-- sim-realized overstock cost running 2.8x (DELI) / 4.0x (DAIRY) above what
-- usp_RecommendParLevels' exponential P(expire) = EXP(-shelf_life/window)
-- term prices. Mechanism (understood, not just measured): safety stock sits
-- BEHIND cycle stock in FIFO, so it only sells in the demand tail and
-- spoils at far above the average-unit rate the exponential assumes -- plus
-- case-pack rounding and phantom-window waste the demand model can't see.
--
-- WHY A PER-FAMILY MULTIPLIER (approach (a)), not a first-principles tail
-- model (approach (b)): the clean closed-form tail model prices the marginal
-- safety unit's overage as Phi(z)*unit_cost, which turns the newsvendor
-- condition into a quadratic in the service level (no circularity) and, on
-- paper, lands DAIRY at z=-0.10 vs the sweep's -0.02 with zero fitting. But
-- it is SHELF-LIFE-BLIND (uses only margin/cost), so it would price
-- GROCERY's 365-day items like a 2-day deli item. A shelf-life-aware tail
-- model reintroduces per-item circularity (P_spoil depends on z depends on
-- P_spoil) that needs per-row Newton iteration -- not clean set-based SQL.
-- So v2 keeps the EXP(-shelf_life/window) shape (correct for long-shelf-life
-- families at multiplier 1.0) and scales it per family by an empirically
-- calibrated multiplier. Families without a sweep default to 1.0 = unchanged
-- from v1; extending the correction to them requires their own sweeps.
--
-- Multipliers back out the sweep's implied Co:
--   k = (Co_implied - carrying_window) / (EXP(-S/W) * unit_cost)
--   DELI : (3.92 - 0.005) / (0.368 * 3.77) = 2.82   [caps at P_spoil=1.0]
--   DAIRY: (0.88 - 0.009) / (0.097 * 2.15) = 4.18
-- The proc caps k*EXP(-S/W) at 1.0 (a probability), so DELI's 2.82 (which
-- would imply 1.04) clamps to "a deli safety unit essentially always
-- spoils" -- which is exactly the finding.
--
-- Idempotent: DROP+CREATE table, unconditional re-populate.

USE PlumDemo;
GO

DROP TABLE IF EXISTS dbo.dim_spoilage_calibration;
GO

CREATE TABLE dbo.dim_spoilage_calibration (
    family               VARCHAR(50)  NOT NULL PRIMARY KEY CLUSTERED,
    spoilage_multiplier  DECIMAL(6,3) NOT NULL,  -- scales EXP(-shelf_life/window); 1.0 = v1 behavior
    source_note          VARCHAR(200) NULL
);
GO

INSERT INTO dbo.dim_spoilage_calibration (family, spoilage_multiplier, source_note) VALUES
    ('DELI',  2.820, 'Calibrated to DELI dollar sweep implied Co $3.92 (2.8x exp model); clamps to P_spoil=1.0 in proc'),
    ('DAIRY', 4.180, 'Calibrated to DAIRY dollar sweep implied Co $0.88 (4.0x exp model)');
GO

PRINT '--- dim_spoilage_calibration ---';
SELECT family, spoilage_multiplier, source_note FROM dbo.dim_spoilage_calibration ORDER BY family;
GO
