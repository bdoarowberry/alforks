CREATE PROCEDURE demo.load_with_variable
AS
BEGIN
    DECLARE @max_a int;

    SET @max_a = (SELECT MAX(m.input_a) FROM demo.source AS m);

    INSERT INTO demo.output (id, result_value)
    SELECT s.id, s.input_b + @max_a
    FROM demo.source AS s;
END;
GO
