-- dbo.Feed and dbo.Feed2 have no definition. Nothing proves colx on dbo.Feed.
CREATE TABLE dbo.Side (id int, colx int);
CREATE TABLE dbo.Out1 (colx int);
GO
CREATE PROCEDURE dbo.report AS
    WITH c AS (SELECT id, colx FROM dbo.Side)
    SELECT s.colx FROM c AS s
    UNION ALL
    SELECT s.id FROM dbo.Feed AS s;
GO
CREATE PROCEDURE dbo.load AS
BEGIN
    WITH d AS (SELECT * FROM dbo.Feed AS f CROSS JOIN dbo.Feed2 AS g)
    INSERT INTO dbo.Out1 (colx) SELECT colx FROM d;
END
GO
CREATE PROCEDURE dbo.fill AS INSERT INTO dbo.Feed (id) VALUES (1);
GO
