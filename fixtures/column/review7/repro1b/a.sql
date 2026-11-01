-- dbo.Feed has no CREATE TABLE; nothing in this SQL shows that it has colx.
CREATE TABLE dbo.Side (id int, colx int);
CREATE TABLE dbo.Outside (id int, colx int);
CREATE TABLE dbo.Out1 (colx int);
GO
CREATE PROCEDURE dbo.report AS
    DECLARE @r TABLE (colx int);
    SELECT s.colx FROM @r AS s WHERE EXISTS (SELECT 1 FROM dbo.Feed AS s);
GO
CREATE PROCEDURE dbo.fill AS INSERT INTO dbo.Feed (id) VALUES (1);
GO
CREATE PROCEDURE dbo.load AS
    INSERT INTO dbo.Out1 (colx)
    SELECT (SELECT MAX(colx) FROM dbo.Feed WHERE id = o.id) FROM dbo.Outside AS o;
GO
