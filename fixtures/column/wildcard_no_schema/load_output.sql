CREATE PROCEDURE demo.load_wildcard
AS
BEGIN
    -- demo.unknown_source has no CREATE TABLE in the scanned files.
    SELECT *
    INTO #copy
    FROM demo.unknown_source;

    INSERT INTO demo.output (id, result_value)
    SELECT c.id, c.input_b
    FROM #copy AS c;
END;
GO
