CREATE TABLE dbo.Out1 (lat float);
GO
CREATE PROCEDURE dbo.w AS
    UPDATE dbo.Feed SET loc.Lat = 1.5;    -- sets property Lat of the UDT column loc
GO
CREATE PROCEDURE dbo.load AS
    WITH d AS (SELECT * FROM dbo.Feed AS f CROSS JOIN dbo.Feed2 AS g)
    INSERT INTO dbo.Out1 (lat) SELECT lat FROM d;
GO
