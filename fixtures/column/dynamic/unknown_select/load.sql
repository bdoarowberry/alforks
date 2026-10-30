CREATE TABLE demo.load_config (id int NOT NULL, table_name sysname NOT NULL);
GO
CREATE PROCEDURE demo.load_configured
AS
BEGIN
    DECLARE @sql nvarchar(max);
    DECLARE @table sysname;

    CREATE TABLE #stage (id int NOT NULL, staged_value int NULL);

    SELECT @table = c.table_name FROM demo.load_config AS c WHERE c.id = 1;
    SET @sql = N'INSERT INTO #stage (id, staged_value) SELECT id, input_a FROM ' + @table;
    EXEC (@sql);

    INSERT INTO demo.output (id, result_value)
    SELECT st.id, st.staged_value
    FROM #stage AS st;
END;
GO
