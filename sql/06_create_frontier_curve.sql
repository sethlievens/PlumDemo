/*
===============================================================================
Create Frontier Curve Table
===============================================================================

Creates the SQL table used by Power BI for the inventory optimization frontier.

The data originates from the calibration sweep generated outside SQL and is
loaded separately.
===============================================================================
*/

USE PlumDemo;
GO

DROP TABLE IF EXISTS dbo.frontier_curve;
GO

CREATE TABLE dbo.frontier_curve (
    frontier_key    INT IDENTITY(1,1) PRIMARY KEY CLUSTERED,
    family          VARCHAR(50)   NOT NULL,
    buffer          DECIMAL(6,2)  NOT NULL,
    waste_pct       DECIMAL(8,4)  NOT NULL,
    days_of_supply  DECIMAL(8,4)  NOT NULL,
    emergency_rate  DECIMAL(8,4)  NOT NULL,
    days_of_stock   DECIMAL(8,4)  NOT NULL
);
GO
