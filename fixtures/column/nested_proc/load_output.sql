CREATE PROCEDURE demo.load_output
AS
BEGIN
    CREATE TABLE #stage (
        id int NOT NULL,
        staged_value int NULL
    );

    EXEC demo.fill_stage;

    INSERT INTO demo.output (id, result_value)
    SELECT st.id, st.staged_value
    FROM #stage AS st;
END;
GO
