CREATE TABLE demo.source (id int, input_a int, input_b int, flag int);
GO
CREATE PROCEDURE demo.load_round3 AS
BEGIN
    DECLARE @lo int = (SELECT MIN(id) FROM demo.source) DECLARE @hi int = (SELECT MAX(id) FROM demo.source)
    CREATE TABLE #t (id int NOT NULL, v int NULL, PRIMARY KEY CLUSTERED (id));
    INSERT INTO #t (id, v)
    SELECT s.id, s.input_a FROM demo.source s WHERE s.flag ! = 0 AND s.id < = @hi;
    ALTER TABLE demo.source ADD PRIMARY KEY CLUSTERED (id);
    INSERT INTO demo.output (id, result_value) SELECT id, v FROM #t;
END
GO
