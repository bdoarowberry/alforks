CREATE PROCEDURE demo.load_wildcard
AS
BEGIN
    SELECT *
    INTO #copy
    FROM demo.source;

    INSERT INTO demo.output (id, result_value)
    SELECT c.id, c.input_b
    FROM #copy AS c;
END;
GO
