CREATE PROCEDURE demo.load_top AS
INSERT INTO demo.output (id, result_value)
SELECT s.id, a.v
FROM demo.source s
CROSS APPLY (SELECT TOP 1 o.v FROM demo.other o WHERE o.id = s.id ORDER BY o.dt DESC) a;
GO
