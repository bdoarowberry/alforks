CREATE TABLE dbo.ni_src (k int);
GO
CREATE TABLE dbo.ni_tgt (k int, d date, n int, ts datetime, g uniqueidentifier, s varchar(10));
GO
CREATE PROCEDURE dbo.load_ni AS
BEGIN
    INSERT INTO dbo.ni_tgt (k, d, n, ts, g, s)
    SELECT k, CONVERT(date, '19000101', 112), CAST(NULL AS int), GETDATE(), NEWID(), LEFT('abcdef', 3)
    FROM dbo.ni_src;
END
GO
