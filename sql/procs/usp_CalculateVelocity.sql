-- =============================================================================
-- usp_CalculateVelocity
--
-- Purpose:
--     Calculates several sales velocity metrics for every
--     Store + Item + Date combination.
--
-- Metrics calculated:
--     • 28-day average sales (excluding promotions and stockouts)
--     • 7-day average sales (actual sales)
--     • Day-of-week sales index
--     • Peer group average sales
--
-- Results are stored in dbo.velocity_daily.
-- =============================================================================


-- Create the output table if it doesn't already exist.
-- This allows the script to be run on a brand-new database.
IF NOT EXISTS (
    SELECT 1
    FROM sys.tables
    WHERE name = 'velocity_daily'
      AND schema_id = SCHEMA_ID('dbo')
)
BEGIN

    -- Identity column is used as the clustered primary key.
    -- Every calculated metric is stored as one row in this table.
    CREATE TABLE dbo.velocity_daily
    (
        velocity_key                INT IDENTITY(1,1) PRIMARY KEY CLUSTERED,

        -- Foreign keys back to the warehouse dimensions
        date_key                    INT NOT NULL,
        store_key                   INT NOT NULL,
        item_key                    INT NOT NULL,
        scenario_key                INT NOT NULL,

        -- Average daily sales over the previous 28 days
        -- (ignoring promotions and stockouts)
        trailing_28d_mean_units     DECIMAL(10,4) NULL,

        -- Indicates whether this weekday normally sells
        -- above or below average.
        -- Example:
        -- Monday = 0.80
        -- Saturday = 1.35
        dow_index                   DECIMAL(10,4) NULL,

        -- Actual 7-day average sales
        trailing_7d_actual_units    DECIMAL(10,4) NULL,

        -- Average velocity of all OTHER stores
        -- in the same peer group.
        peer_group_mean_units       DECIMAL(10,4) NULL,

        -- Timestamp showing when these metrics were calculated.
        calculated_at               DATETIME2 NOT NULL
    );
END
GO


DROP PROCEDURE IF EXISTS dbo.usp_CalculateVelocity;
GO


CREATE PROCEDURE dbo.usp_CalculateVelocity
    @scenario_key INT
AS
BEGIN

    -- Prevent SQL Server from returning
    -- "(123 rows affected)" after every statement.
    -- Makes procedures slightly faster and cleaner.
    SET NOCOUNT ON;

    -------------------------------------------------------------------------
    -- Remove any previous calculations for this scenario.
    -- This lets us completely rebuild the velocity table.
    -------------------------------------------------------------------------
    DELETE
    FROM dbo.velocity_daily
    WHERE scenario_key = @scenario_key;


    ;WITH

    -------------------------------------------------------------------------
    -- clean_sales
    --
    -- Pull together sales, inventory, store, and date information.
    --
    -- Also create two "cleaned" versions of Units Sold:
    --
    -- clean_units
    --     Excludes promotional sales and stockouts because those
    --     aren't considered normal demand.
    --
    -- dow_units
    --     Excludes only stockouts.
    --     Promotional sales are kept because we only care about
    --     weekday selling patterns.
    -------------------------------------------------------------------------
    clean_sales AS
    (
        SELECT

            fs.date_key,
            fs.store_key,
            fs.item_key,

            dd.day_of_week,
            ds.peer_group_id,

            fs.units_sold,

            CASE
                WHEN fs.on_promo_flag = 1
                  OR fi.on_hand_units = 0
                THEN NULL
                ELSE fs.units_sold
            END AS clean_units,

            CASE
                WHEN fi.on_hand_units = 0
                THEN NULL
                ELSE fs.units_sold
            END AS dow_units

        FROM dbo.fact_sales fs

        JOIN dbo.fact_inventory_snap fi
            ON fi.date_key = fs.date_key
           AND fi.store_key = fs.store_key
           AND fi.item_key = fs.item_key
           AND fi.scenario_key = fs.scenario_key

        JOIN dbo.dim_date dd
            ON dd.date_key = fs.date_key

        JOIN dbo.dim_store ds
            ON ds.store_key = fs.store_key

        WHERE fs.scenario_key = @scenario_key
    ),


    -------------------------------------------------------------------------
    -- windowed
    --
    -- Calculate rolling averages using SQL window functions.
    --
    -- Window functions calculate values across neighboring rows
    -- without collapsing the data into GROUP BY results.
    -------------------------------------------------------------------------
    windowed AS
    (
        SELECT

            date_key,
            store_key,
            item_key,
            day_of_week,
            peer_group_id,

            -----------------------------------------------------------------
            -- Rolling 28-day average.
            --
            -- Looks at the current row plus the previous 27 rows.
            -----------------------------------------------------------------
            AVG(clean_units) OVER
            (
                PARTITION BY store_key, item_key
                ORDER BY date_key
                ROWS BETWEEN 27 PRECEDING AND CURRENT ROW
            ) AS trailing_28d_mean_units,

            -----------------------------------------------------------------
            -- Rolling 7-day average using actual sales.
            -----------------------------------------------------------------
            AVG(units_sold) OVER
            (
                PARTITION BY store_key, item_key
                ORDER BY date_key
                ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
            ) AS trailing_7d_actual_units,

            -----------------------------------------------------------------
            -- Historical average for THIS weekday only.
            --
            -- Example:
            -- If today is Monday, average all previous Mondays.
            --
            -- "1 PRECEDING" excludes today's row so we don't
            -- accidentally use today's sales to predict today.
            -----------------------------------------------------------------
            AVG(dow_units) OVER
            (
                PARTITION BY store_key, item_key, day_of_week
                ORDER BY date_key
                ROWS BETWEEN UNBOUNDED PRECEDING
                         AND 1 PRECEDING
            ) AS dow_mean_prior,

            -----------------------------------------------------------------
            -- Historical overall average across all previous days.
            -----------------------------------------------------------------
            AVG(dow_units) OVER
            (
                PARTITION BY store_key, item_key
                ORDER BY date_key
                ROWS BETWEEN UNBOUNDED PRECEDING
                         AND 1 PRECEDING
            ) AS overall_mean_prior

        FROM clean_sales
    ),


    -------------------------------------------------------------------------
    -- with_peer
    --
    -- Calculate final derived metrics.
    -------------------------------------------------------------------------
    with_peer AS
    (
        SELECT

            date_key,
            store_key,
            item_key,

            trailing_28d_mean_units,
            trailing_7d_actual_units,

            -----------------------------------------------------------------
            -- Day-of-week index.
            --
            -- Formula:
            --
            -- Average Monday Sales
            -- --------------------
            -- Overall Average Sales
            --
            -- Values:
            -- 1.00 = average
            -- >1   = stronger than average
            -- <1   = weaker than average
            --
            -- NULLIF prevents divide-by-zero errors.
            -----------------------------------------------------------------
            dow_mean_prior
            /
            NULLIF(overall_mean_prior, 0)
            AS dow_index,

            -----------------------------------------------------------------
            -- Peer group average.
            --
            -- We subtract this store's value so the comparison
            -- only includes OTHER stores in the peer group.
            -----------------------------------------------------------------
            (
                SUM(trailing_28d_mean_units)
                OVER (PARTITION BY item_key, date_key, peer_group_id)

                - trailing_28d_mean_units
            )

            /

            NULLIF
            (
                COUNT(*)
                OVER (PARTITION BY item_key, date_key, peer_group_id)

                - 1,
                0
            )

            AS peer_group_mean_units

        FROM windowed
    )

    -------------------------------------------------------------------------
    -- Store all calculated metrics.
    -------------------------------------------------------------------------
    INSERT INTO dbo.velocity_daily
    (
        date_key,
        store_key,
        item_key,
        scenario_key,
        trailing_28d_mean_units,
        dow_index,
        trailing_7d_actual_units,
        peer_group_mean_units,
        calculated_at
    )

    SELECT

        date_key,
        store_key,
        item_key,

        @scenario_key,

        trailing_28d_mean_units,
        dow_index,
        trailing_7d_actual_units,
        peer_group_mean_units,

        -- Save the timestamp of this calculation.
        SYSUTCDATETIME()

    FROM with_peer;

    -------------------------------------------------------------------------
    -- Return the number of rows inserted.
    -------------------------------------------------------------------------
    SELECT @@ROWCOUNT AS rows_inserted;

END
GO