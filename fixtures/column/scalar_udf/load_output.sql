-- dbo.fn_adjust is not defined in the scanned files.
CREATE PROCEDURE demo.load_udf
AS
BEGIN
    INSERT INTO demo.output (id, result_value)
    SELECT s.id, dbo.fn_adjust(s.input_a)
    FROM demo.source AS s;
END;
GO
