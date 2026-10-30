CREATE PROCEDURE demo.load_dynamic
    @column_name sysname
AS
BEGIN
    DECLARE @sql nvarchar(max);

    SET @sql = N'INSERT INTO demo.output (id, result_value) SELECT id, '
             + QUOTENAME(@column_name)
             + N' FROM demo.source';

    EXEC sp_executesql @sql;
END;
GO
