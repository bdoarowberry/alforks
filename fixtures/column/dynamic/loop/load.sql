CREATE PROCEDURE demo.load_looped
AS
BEGIN
    DECLARE @sql nvarchar(max) = N'';
    DECLARE @i int = 0;

    CREATE TABLE #stage (id int NOT NULL, staged_value int NULL);

    WHILE @i < 2
    BEGIN
        SET @sql = @sql + N'INSERT INTO #stage (id, staged_value) SELECT id, input_a FROM demo.source; ';
        SET @i = @i + 1;
    END;
    EXEC (@sql);

    INSERT INTO demo.output (id, result_value)
    SELECT st.id, st.staged_value
    FROM #stage AS st;
END;
GO
