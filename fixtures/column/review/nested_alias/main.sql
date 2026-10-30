CREATE PROCEDURE demo.load_nested AS
INSERT INTO demo.output (id, result_value)
SELECT x.id, x.v
FROM (
    SELECT x.id, x.v + 1 AS v
    FROM (SELECT s.id, s.input_a AS v FROM demo.source s WHERE s.k = 1) x
) x;
GO
