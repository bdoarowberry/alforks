CREATE PROCEDURE demo.load_case
AS
BEGIN
    INSERT INTO demo.output (id, result_value, alt_value)
    SELECT
        s.id,
        CASE
            WHEN s.use_b = 1 THEN s.input_b
            ELSE s.input_a
        END,
        CASE WHEN s.use_b = 1 THEN s.input_b END
    FROM demo.source AS s;
END;
GO
