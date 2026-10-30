CREATE PROCEDURE demo.load_where AS
INSERT INTO demo.output (id, result_value)
SELECT s.id, s.input_a
FROM demo.source s
WHERE s.input_b > (SELECT AVG(o.v) FROM demo.other o WHERE o.k = s.k);
GO
