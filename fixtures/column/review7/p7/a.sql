CREATE TABLE dbo.Outside (id int, colx int);
CREATE TABLE dbo.Out1 (colx int);
GO
CREATE PROCEDURE dbo.old_writer AS
    INSERT INTO dbo.Feed (id, colx) VALUES (1, 2);
GO
ALTER TABLE dbo.Feed DROP COLUMN colx;
GO
CREATE PROCEDURE dbo.load AS
    INSERT INTO dbo.Out1 (colx)
    SELECT (SELECT MAX(colx) FROM dbo.Feed WHERE id = o.id) FROM dbo.Outside AS o;
GO
