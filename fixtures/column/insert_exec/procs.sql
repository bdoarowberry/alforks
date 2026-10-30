CREATE PROCEDURE demo.produce_rows
AS
BEGIN
    SELECT s.id, s.input_a
    FROM demo.source AS s;
END;
GO

CREATE PROCEDURE demo.load_output
AS
BEGIN
    INSERT INTO demo.output (id, result_value)
    EXEC demo.produce_rows;
END;
GO
