CREATE PROCEDURE demo.load_dynamic
    @source_name sysname
AS
BEGIN
    DECLARE @sql nvarchar(max);

    CREATE TABLE #stage (
        id int NOT NULL,
        staged_value int NULL
    );

    SET @sql = N'INSERT INTO #stage (id, staged_value) SELECT id, input_a FROM ' + QUOTENAME(@source_name);
    EXEC (@sql);

    INSERT INTO demo.output (id, result_value)
    SELECT st.id, st.staged_value
    FROM #stage AS st;
END;
GO
