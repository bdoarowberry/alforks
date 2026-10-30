CREATE TABLE demo.source (id int, input_a int);
GO
CREATE PROCEDURE demo.load_q AS
BEGIN
    CREATE TABLE #t (id int, result_value int);
    INSERT INTO #t (id, t.result_value) SELECT s.id, s.input_a FROM demo.source s;
    INSERT INTO demo.output (id, result_value) SELECT id, result_value FROM #t;
END
GO
