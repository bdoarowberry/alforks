CREATE TABLE demo.source_x (id int, col_a int, flag int);
GO
CREATE TABLE demo.source_y (id int, col_b int);
GO
CREATE TABLE demo.column_config (source_table sysname, source_column sysname);
GO
CREATE PROCEDURE demo.load_dynamic AS
BEGIN
    CREATE TABLE #stage (id int, metric int);
    DECLARE @src sysname, @col sysname, @sql nvarchar(max);
    SELECT @src = source_table, @col = source_column FROM demo.column_config;
    SET @sql = N'INSERT INTO #stage (id, metric) SELECT id, ' + QUOTENAME(@col) + N' FROM ' + @src;
    EXEC sp_executesql @sql;
    INSERT INTO demo.output (id, result_value) SELECT id, metric FROM #stage;
END
GO
