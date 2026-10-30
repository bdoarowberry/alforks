CREATE PROCEDURE demo.update_from_b
AS
BEGIN
    UPDATE demo.output
    SET result_value = s.input_b
    FROM demo.output
    INNER JOIN demo.source AS s ON s.id = demo.output.id
    WHERE s.input_b IS NOT NULL;
END;
GO
