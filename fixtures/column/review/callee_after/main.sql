CREATE TABLE demo.source (id int, input_a int);
GO
CREATE TABLE demo.other (id int, v int);
GO
CREATE TABLE demo.output (id int, result_value int);
GO
CREATE PROCEDURE demo.fill_t AS
INSERT INTO #t (id, v) SELECT o.id, o.v FROM demo.other o;
GO
CREATE PROCEDURE demo.load_ca AS
BEGIN
    CREATE TABLE #t (id int, v int);
    INSERT INTO #t (id, v) SELECT s.id, s.input_a FROM demo.source s;
    INSERT INTO demo.output (id, result_value) SELECT id, v FROM #t;
    EXEC demo.fill_t;
END
GO
