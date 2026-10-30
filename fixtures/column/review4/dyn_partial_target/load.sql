CREATE TABLE dbo.target (k int, c int);
CREATE TABLE dbo.feed (k int, v int);
GO
CREATE PROCEDURE dbo.load_target @tbl sysname AS
BEGIN
    DECLARE @sql nvarchar(max);
    INSERT INTO dbo.target (k, c) SELECT f.k, f.v FROM dbo.feed f;
    SET @sql = N'UPDATE ' + QUOTENAME(@tbl) + N' SET c = c * 100';
    EXEC (@sql);
END
GO
