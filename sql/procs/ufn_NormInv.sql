-- =============================================================================
-- Function: ufn_NormInv
--
-- Purpose
-- -------
-- Inverse standard-normal CDF: the "z-score for a probability" lookup.
-- Given a probability p in (0, 1), returns the z where P(Z <= z) = p.
--
-- The par-level engine uses this to turn an item's newsvendor critical ratio
-- into the z-score that sizes its safety stock. SQL Server has no native
-- equivalent (NORMSINV is Excel-only).
--
-- Inputs
-- ------
-- @p
--     Target cumulative probability. Expected in (0, 1).
--
-- Outputs
-- -------
-- Returns the z-score (FLOAT). Inputs at or beyond the (0, 1) bounds return a
-- large +/- sentinel rather than erroring, so callers can clamp them off with
-- GREATEST / LEAST.
--
-- Notes
-- -----
-- Peter Acklam's rational approximation (relative error < 1.15e-9). A single
-- polynomial cannot fit the whole curve, so the domain is split into a lower
-- tail, a central band, and an upper tail. Validated against published tables:
-- ufn_NormInv(0.975) = 1.959964 vs the textbook 1.96.
-- =============================================================================

DROP FUNCTION IF EXISTS dbo.ufn_NormInv;
GO

CREATE FUNCTION dbo.ufn_NormInv(@p FLOAT)
RETURNS FLOAT
AS
BEGIN
    -- Out-of-domain guard: return a sentinel instead of failing on log(0).
    IF @p <= 0.0 RETURN -1e10;
    IF @p >= 1.0 RETURN 1e10;

    -- Published approximation coefficients (Acklam).
    DECLARE @a1 FLOAT = -3.969683028665376e+01, @a2 FLOAT =  2.209460984245205e+02,
            @a3 FLOAT = -2.759285104469687e+02, @a4 FLOAT =  1.383577518672690e+02,
            @a5 FLOAT = -3.066479806614716e+01, @a6 FLOAT =  2.506628277459239e+00;
    DECLARE @b1 FLOAT = -5.447609879822406e+01, @b2 FLOAT =  1.615858368580409e+02,
            @b3 FLOAT = -1.556989798598866e+02, @b4 FLOAT =  6.680131188771972e+01,
            @b5 FLOAT = -1.328068155288572e+01;
    DECLARE @c1 FLOAT = -7.784894002430293e-03, @c2 FLOAT = -3.223964580411365e-01,
            @c3 FLOAT = -2.400758277161838e+00, @c4 FLOAT = -2.549732539343734e+00,
            @c5 FLOAT =  4.374664141464968e+00, @c6 FLOAT =  2.938163982698783e+00;
    DECLARE @d1 FLOAT =  7.784695709041462e-03, @d2 FLOAT =  3.224671290700398e-01,
            @d3 FLOAT =  2.445134137142996e+00, @d4 FLOAT =  3.754408661907416e+00;

    -- Breakpoints between the tail regions and the central band.
    DECLARE @p_low FLOAT = 0.02425, @p_high FLOAT;
    DECLARE @q FLOAT, @r FLOAT, @x FLOAT;
    SET @p_high = 1.0 - @p_low;

    IF @p < @p_low
    BEGIN
        -- Lower tail.
        SET @q = SQRT(-2.0 * LOG(@p));
        SET @x = (((((@c1*@q+@c2)*@q+@c3)*@q+@c4)*@q+@c5)*@q+@c6) / ((((@d1*@q+@d2)*@q+@d3)*@q+@d4)*@q+1.0);
    END
    ELSE IF @p <= @p_high
    BEGIN
        -- Central band.
        SET @q = @p - 0.5;
        SET @r = @q * @q;
        SET @x = (((((@a1*@r+@a2)*@r+@a3)*@r+@a4)*@r+@a5)*@r+@a6)*@q / (((((@b1*@r+@b2)*@r+@b3)*@r+@b4)*@r+@b5)*@r+1.0);
    END
    ELSE
    BEGIN
        -- Upper tail.
        SET @q = SQRT(-2.0 * LOG(1.0 - @p));
        SET @x = -(((((@c1*@q+@c2)*@q+@c3)*@q+@c4)*@q+@c5)*@q+@c6) / ((((@d1*@q+@d2)*@q+@d3)*@q+@d4)*@q+1.0);
    END

    RETURN @x;
END
GO
