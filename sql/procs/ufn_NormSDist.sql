-- =============================================================================
-- Function: ufn_NormSDist
--
-- Purpose
-- -------
-- Standard-normal CDF: the "probability for a z-score" lookup, and the inverse
-- of ufn_NormInv. Given a z-score, returns P(Z <= z).
--
-- The par-level engine uses this to report a readable cycle service level from
-- the z it used to size safety stock (z = 0 -> 50%, z = 1.65 -> ~95%). z can be
-- negative (deliberate under-stocking), which returns a value below 50%. SQL
-- Server has no native equivalent (NORMSDIST is Excel-only).
--
-- Inputs
-- ------
-- @z
--     The z-score (any real number).
--
-- Outputs
-- -------
-- Returns the cumulative probability P(Z <= z), a FLOAT in (0, 1).
--
-- Notes
-- -----
-- Zelen & Severo rational approximation (Abramowitz & Stegun 26.2.17), max
-- absolute error 7.5e-8. The formula is defined for the right half of the
-- curve, so negative z is evaluated on |z| and reflected at the end.
-- =============================================================================

DROP FUNCTION IF EXISTS dbo.ufn_NormSDist;
GO

CREATE FUNCTION dbo.ufn_NormSDist(@z FLOAT)
RETURNS FLOAT
AS
BEGIN
    -- Published approximation coefficients.
    DECLARE @b0 FLOAT = 0.2316419;
    DECLARE @b1 FLOAT =  0.319381530, @b2 FLOAT = -0.356563782, @b3 FLOAT = 1.781477937,
            @b4 FLOAT = -1.821255978, @b5 FLOAT =  1.330274429;

    DECLARE @x FLOAT = ABS(@z);                                     -- evaluate on the right half; reflect below
    DECLARE @t FLOAT = 1.0 / (1.0 + @b0 * @x);                      -- change of variable the polynomial is written in
    DECLARE @phi FLOAT = (1.0 / SQRT(2.0 * PI())) * EXP(-@x * @x / 2.0);  -- normal density at x
    DECLARE @poly FLOAT = @t * (@b1 + @t * (@b2 + @t * (@b3 + @t * (@b4 + @t * @b5))));
    DECLARE @cdf_pos FLOAT = 1.0 - @phi * @poly;                    -- P(Z <= x) for x >= 0

    RETURN CASE WHEN @z >= 0 THEN @cdf_pos ELSE 1.0 - @cdf_pos END; -- reflect for negative z
END
GO
