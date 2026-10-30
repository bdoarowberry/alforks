CREATE PROCEDURE demo.refresh_output
AS
BEGIN
    UPDATE t
    SET t.result_value = s.input_a
    FROM demo.output AS t
    INNER JOIN demo.source AS s ON s.id = t.id;

    UPDATE t
    SET t.fallback_value = COALESCE(s.input_a, s.input_b, 0)
    FROM demo.output AS t
    INNER JOIN demo.source AS s ON s.id = t.id;
END;
GO
