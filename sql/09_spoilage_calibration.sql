/*
===============================================================================
Spoilage Cost Calibration
===============================================================================

Creates the calibration table used by the par-level recommendation engine to
adjust spoilage risk by product family.

The base spoilage probability model uses shelf life and demand window. This
table applies empirically derived family-level adjustments based on simulation
results.

Families without a calibration entry continue using the default model behavior.
===============================================================================
*/

USE PlumDemo;
GO


DROP TABLE IF EXISTS dbo.dim_spoilage_calibration;
GO


CREATE TABLE dbo.dim_spoilage_calibration
(
    family VARCHAR(50) NOT NULL,

    -- Multiplier applied to the base spoilage probability calculation.
    -- 1.0 represents the original model behavior.
    spoilage_multiplier DECIMAL(6,3) NOT NULL,

    source_note VARCHAR(200) NULL,

    CONSTRAINT PK_dim_spoilage_calibration
        PRIMARY KEY CLUSTERED (family)
);
GO


/*
    Calibrated adjustments based on simulation results.

    DELI and DAIRY showed higher realized spoilage costs than the original
    shelf-life decay model predicted.
*/

INSERT INTO dbo.dim_spoilage_calibration
(
    family,
    spoilage_multiplier,
    source_note
)
VALUES
(
    'DELI',
    2.820,
    'Calibrated from simulation results'
),
(
    'DAIRY',
    4.180,
    'Calibrated from simulation results'
);
GO


PRINT 'Spoilage calibration table created.';

SELECT
    family,
    spoilage_multiplier,
    source_note
FROM dbo.dim_spoilage_calibration
ORDER BY family;
GO
