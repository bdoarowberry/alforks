CREATE TABLE stage.src (id int, code varchar(10));
GO
CREATE TABLE dbo.out (id int, code varchar(10));
GO
CREATE PROCEDURE dbo.load_out AS
INSERT INTO dbo.out (id, code) SELECT s.id, s.code FROM stage.src s;
GO
-- The only writer of stage.src is dynamic SQL whose value is not known statically.
CREATE PROCEDURE stage.refresh_src @filter nvarchar(200) AS
BEGIN
    DECLARE @sql nvarchar(max) = N'INSERT INTO stage.src (id, code) SELECT id, code FROM stage.feed WHERE ' + @filter;
    EXEC sp_executesql @sql;
END
GO
