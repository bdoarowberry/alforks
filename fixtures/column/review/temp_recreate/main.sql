CREATE PROCEDURE demo.load_recreate AS
BEGIN
    SELECT s.id, s.input_a AS v INTO #t FROM demo.source s;
    INSERT INTO demo.output (id, other_value) SELECT id, v FROM #t;
    DROP TABLE #t;
    SELECT o.id, o.v AS v INTO #t FROM demo.other o;
    INSERT INTO demo.output (id, result_value) SELECT id, v FROM #t;
END
GO
