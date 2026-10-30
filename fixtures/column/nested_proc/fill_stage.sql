-- Fills a #temp table that its caller creates.
CREATE PROCEDURE demo.fill_stage
AS
BEGIN
    INSERT INTO #stage (id, staged_value)
    SELECT s.id, s.input_a
    FROM demo.source AS s;
END;
GO
