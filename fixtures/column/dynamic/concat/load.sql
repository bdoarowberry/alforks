CREATE PROCEDURE demo.load_concat
AS
BEGIN
    DECLARE @sql nvarchar(max);
    DECLARE @table sysname = N'source';
    DECLARE @columns nvarchar(200);

    CREATE TABLE #stage (id int NOT NULL, staged_value int NULL);

    SET @columns = N'id, ' + N'input_a';
    SET @sql = N'INSERT INTO #stage (id, staged_value) ';
    SET @sql = @sql + N'SELECT ' + @columns + N' FROM demo.' + @table;
    EXEC sp_executesql @sql;

    INSERT INTO demo.output (id, result_value)
    SELECT st.id, st.staged_value
    FROM #stage AS st;
END;
GO

CREATE PROCEDURE demo.load_concat_function
AS
BEGIN
    DECLARE @sql nvarchar(max) = CONCAT(N'INSERT INTO demo.output_concat (id, result_value) SELECT id, ', N'input_b', NULL,
                                        N' FROM demo.source');
    EXECUTE (@sql);
END;
GO
CREATE TABLE demo.output_concat (id int NOT NULL, result_value int NULL);
GO
