CREATE PROCEDURE demo.load_derived
AS
BEGIN
    INSERT INTO demo.output (id, result_value)
    SELECT d.id, d.renamed_value
    FROM (
        SELECT s.id, s.input_a AS renamed_value
        FROM demo.source AS s
    ) AS d;
END;
GO
