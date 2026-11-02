CREATE TABLE dbo.Tgt (k int NOT NULL, v int NULL);
CREATE TABLE dbo.A (k int NOT NULL, x int NULL);
CREATE TABLE dbo.B (k int NOT NULL, y int NULL);
GO
CREATE PROCEDURE dbo.w_one AS
BEGIN
    INSERT dbo.Tgt (k, v) SELECT k, x FROM dbo.A;
    UPDATE t SET v = a.x FROM dbo.Tgt t JOIN dbo.A a ON a.k = t.k;
END
GO
CREATE PROCEDURE dbo.w_two @col sysname AS
BEGIN
    DECLARE @sql nvarchar(max) = N'UPDATE t SET v = b.' + QUOTENAME(@col) + N' FROM dbo.Tgt t JOIN dbo.B b ON b.k = t.k';
    EXEC (@sql);
END
