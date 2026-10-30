CREATE TABLE dbo.target (k int, c int);
CREATE TABLE dbo.feed (k int, v int);
GO
CREATE PROCEDURE dbo.generic_copy @dest sysname, @src sysname AS
BEGIN
    DECLARE @stmt nvarchar(max) = CONCAT(N'INSERT INTO ', @dest, N' SELECT * FROM ', @src);
    EXEC sp_executesql @stmt;
END
GO
EXEC dbo.generic_copy @dest = N'dbo.target', @src = N'dbo.feed';
GO
