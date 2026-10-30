CREATE PROCEDURE demo.load_output
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        s.id,
        s.input_a AS staged_value
    INTO #stage
    FROM demo.source AS s;

    INSERT INTO demo.output (id, result_value)
    SELECT st.id, st.staged_value
    FROM #stage AS st;
END;
GO
