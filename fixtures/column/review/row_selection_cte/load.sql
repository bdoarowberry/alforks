CREATE TABLE demo.source (id int, input_a int);
GO
CREATE PROCEDURE demo.load_filtered AS
BEGIN
    WITH active AS (
        SELECT f.* FROM demo.flags_without_schema AS f WHERE f.is_active = 1
    )
    INSERT INTO demo.output (id, result_value)
    SELECT s.id, s.input_a
    FROM demo.source AS s
    JOIN active AS a ON a.id = s.id;
END
GO
