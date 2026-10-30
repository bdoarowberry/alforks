CREATE PROCEDURE demo.load_quoted
AS
BEGIN
    DECLARE @sql nvarchar(max);
    DECLARE @template nvarchar(max) = N'INSERT INTO {target} (id, result_value)' + CHAR(13) + CHAR(10) + N'SELECT id, {column} FROM {source}';

    SET @sql = REPLACE(@template, N'{target}', QUOTENAME(N'demo') + N'.' + QUOTENAME(N'output'));
    SET @sql = REPLACE(@sql, N'{source}', QUOTENAME(N'demo') + N'.' + QUOTENAME(N'source'));
    SET @sql = REPLACE(@sql, N'{COLUMN}', CAST(N'input_a' AS nvarchar(20)));
    EXEC sp_executesql @sql;
END;
GO
