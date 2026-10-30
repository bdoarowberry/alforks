CREATE PROCEDURE demo.load_hinted
AS
BEGIN
    -- The OPTION (USE HINT (...)) query hint is not parsed by the SQL parser.
    INSERT INTO demo.output (id, result_value)
    SELECT s.id, s.input_a
    FROM demo.source AS s
    OPTION (USE HINT ('FORCE_LEGACY_CARDINALITY_ESTIMATION'));
END;
GO

CREATE PROCEDURE demo.update_with_output
AS
BEGIN
    CREATE TABLE #changed (id int NOT NULL);

    -- UPDATE ... OUTPUT ... INTO is not parsed by the SQL parser.
    UPDATE demo.output
    SET other_value = s.input_a
    OUTPUT inserted.id INTO #changed (id)
    FROM demo.output
    INNER JOIN demo.source AS s ON s.id = demo.output.id;
END;
GO
