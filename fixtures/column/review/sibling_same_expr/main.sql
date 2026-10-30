CREATE PROCEDURE demo.load_sib2 AS
INSERT INTO demo.output (id, result_value)
SELECT x.id, x.v + (SELECT MAX(x.v) FROM (SELECT o.v FROM demo.other o) x)
FROM (SELECT s.id, s.input_a AS v FROM demo.source s) x;
GO
