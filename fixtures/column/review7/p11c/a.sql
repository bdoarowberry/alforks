CREATE TABLE dbo.Out1 (colx int);
CREATE TABLE dbo.Side (id int, colx int);
CREATE TABLE dbo.Side2 (id int, coly int);
GO
CREATE PROCEDURE dbo.a AS
BEGIN
    SELECT * INTO #t FROM dbo.Side2;
    WITH d AS (SELECT * FROM #t CROSS JOIN dbo.Feed2 AS g)
    INSERT INTO dbo.Out1 (colx) SELECT colx FROM d;
    DROP TABLE #t;
    SELECT id, colx INTO #t FROM dbo.Side;
    INSERT INTO #t (colx) VALUES (1);
END
GO
