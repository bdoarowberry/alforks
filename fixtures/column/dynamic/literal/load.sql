CREATE PROCEDURE demo.load_literal
AS
BEGIN
    CREATE TABLE #stage (id int NOT NULL, staged_value int NULL);

    EXEC (N'INSERT INTO #stage (id, staged_value) SELECT id, input_a FROM demo.source');

    INSERT INTO demo.output (id, result_value)
    SELECT st.id, st.staged_value
    FROM #stage AS st;
END;
GO
