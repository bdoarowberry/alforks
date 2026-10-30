CREATE PROCEDURE demo.load_cte
AS
BEGIN
    WITH base AS (
        SELECT s.id, s.input_a AS base_value
        FROM demo.source AS s
        WHERE s.is_active = 1
    ),
    doubled AS (
        SELECT b.id, b.base_value * 2 AS doubled_value
        FROM base AS b
    ),
    final_cte AS (
        SELECT d.id, d.doubled_value AS final_value
        FROM doubled AS d
    )
    INSERT INTO demo.output (id, result_value)
    SELECT f.id, f.final_value
    FROM final_cte AS f;
END;
GO
