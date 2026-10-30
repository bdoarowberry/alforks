CREATE PROCEDURE demo.load_positional
AS
BEGIN
    INSERT INTO demo.output
    SELECT s.input_b, s.input_a
    FROM demo.source AS s;
END;
GO
