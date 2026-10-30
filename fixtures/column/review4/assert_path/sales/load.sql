CREATE PROCEDURE dbo.p_sales_load_sql @cols nvarchar(max) AS
BEGIN
    DECLARE @sql nvarchar(max);
    SET @sql = N'INSERT INTO dbo.target (k, c) SELECT ' + @cols + N' FROM dbo.feed';
    EXEC (@sql);
END
GO
