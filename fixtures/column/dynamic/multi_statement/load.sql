CREATE PROCEDURE demo.load_two_steps
AS
BEGIN
    DECLARE @sql nvarchar(max);
    SET @sql = N'SELECT s.id, s.input_a AS step_value INTO #work FROM demo.source AS s;
INSERT INTO demo.output (id, result_value)
SELECT w.id, w.step_value * 2 FROM #work AS w;
DROP TABLE #work;';
    EXEC (@sql);
END;
GO
