CREATE PROCEDURE demo.load_alter AS
BEGIN
    SELECT s.id INTO #t FROM demo.source s;
    ALTER TABLE #t ADD extra int NULL;
    UPDATE t SET t.extra = o.v FROM #t t JOIN demo.other o ON o.id = t.id;
    INSERT INTO demo.output (id, result_value) SELECT t.id, t.extra FROM #t t;
END
GO
