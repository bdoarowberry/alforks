CREATE PROCEDURE demo.merge_d AS
WITH agg AS (SELECT o.id, SUM(o.v) AS total FROM demo.other o GROUP BY o.id)
MERGE demo.output AS t
USING (SELECT a.id, a.total, s.input_b FROM agg a JOIN demo.source s ON s.id = a.id) AS src
ON t.id = src.id
WHEN MATCHED AND src.input_b > 0 THEN UPDATE SET t.result_value = t.result_value + src.total
WHEN NOT MATCHED THEN INSERT (id, result_value) VALUES (src.id, src.total);
GO
