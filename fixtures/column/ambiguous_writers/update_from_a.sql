CREATE PROCEDURE demo.update_from_a
AS
BEGIN
    UPDATE o
    SET o.result_value = s.input_a
    FROM demo.output AS o
    INNER JOIN demo.source AS s ON s.id = o.id;
END;
GO
