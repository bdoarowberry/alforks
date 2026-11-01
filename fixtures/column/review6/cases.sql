-- H1: a UNION branch after an unexpanded * must not be dropped.
CREATE PROCEDURE dbo.p_union AS
BEGIN
    WITH c AS (SELECT * FROM dbo.Twice UNION ALL SELECT id, col_a FROM dbo.Other)
    INSERT INTO dbo.Out_u (col_a) SELECT col_a FROM c;
    SELECT * INTO #u FROM dbo.Twice UNION ALL SELECT id, col_a FROM dbo.Other;
    INSERT INTO dbo.Out_v (col_a) SELECT col_a FROM #u;
END
GO
-- H2: the call argument s.code is the caller's s, not the function's own alias s.
CREATE FUNCTION dbo.Fn1 (@k int) RETURNS TABLE AS RETURN (SELECT @k AS pid, s.col_a FROM dbo.Child AS s);
GO
CREATE PROCEDURE dbo.p_shadow AS
    INSERT INTO dbo.Out_shadow (pid)
    SELECT x.pid FROM dbo.Parent AS s CROSS APPLY (SELECT * FROM dbo.Fn1(s.code)) AS x;
GO
-- H3: an unqualified name in the body never binds to the caller.
CREATE FUNCTION dbo.FnEscape (@k int) RETURNS TABLE AS RETURN (SELECT code FROM dbo.Narrow WHERE id = @k);
GO
CREATE PROCEDURE dbo.p_escape AS
    INSERT INTO dbo.Out_escape (code)
    SELECT x.code FROM dbo.Parent AS p CROSS APPLY (SELECT * FROM dbo.FnEscape(p.id)) AS x;
GO
-- H4: a function of another database is not inlined against this one.
CREATE PROCEDURE dbo.p_other_db AS
    INSERT INTO dbo.Out_other_db (col_a) SELECT f.col_a FROM OtherDb.dbo.FnOther(1) AS f;
GO
-- H5: a CTE in the RETURN query.
CREATE FUNCTION dbo.FnCte (@k int) RETURNS TABLE AS
RETURN WITH Twice AS (SELECT col_a FROM dbo.Src WHERE id = @k) SELECT Twice.col_a FROM Twice;
GO
CREATE PROCEDURE dbo.p_fn_cte AS
    INSERT INTO dbo.Out_fn_cte (col_a) SELECT x.col_a FROM dbo.Parent AS p CROSS APPLY (SELECT * FROM dbo.FnCte(p.id)) AS x;
GO
-- H6: statements other than SELECT that return rows.
CREATE PROCEDURE dbo.C_del AS
BEGIN
    DELETE FROM dbo.Log OUTPUT deleted.col_a WHERE id = 1;
    SELECT col_a FROM dbo.Src;
END
GO
CREATE PROCEDURE dbo.C_fetch AS
BEGIN
    DECLARE c CURSOR FOR SELECT col_b FROM dbo.Src;
    OPEN c;
    FETCH NEXT FROM c;
    SELECT col_a FROM dbo.Src;
END
GO
CREATE PROCEDURE dbo.R_rows AS
BEGIN
    CREATE TABLE #r1 (col_a int);
    INSERT INTO #r1 (col_a) EXEC dbo.C_del;
    INSERT INTO dbo.Out_del (col_a) SELECT col_a FROM #r1;
    CREATE TABLE #r2 (col_a int);
    INSERT INTO #r2 (col_a) EXEC dbo.C_fetch;
    INSERT INTO dbo.Out_fetch (col_a) SELECT col_a FROM #r2;
END
GO
-- H7: the callee reads the calling procedure's #x, not another caller's.
CREATE PROCEDURE dbo.C_reads AS SELECT v FROM #x;
GO
CREATE PROCEDURE dbo.A AS
BEGIN
    CREATE TABLE #x (v int);
    INSERT INTO #x (v) SELECT col_a FROM dbo.Src;
    CREATE TABLE #r (v int);
    INSERT INTO #r (v) EXEC dbo.C_reads;
    INSERT INTO dbo.Out_caller_temp (v) SELECT v FROM #r;
END
GO
CREATE PROCEDURE dbo.B AS
BEGIN
    CREATE TABLE #x (v int);
    INSERT INTO #x (v) SELECT col_b FROM dbo.Src;
    EXEC dbo.C_reads;
END
GO
-- H8: a #temp created only when missing is the caller's table when the caller has one.
CREATE PROCEDURE dbo.Helper (@cmd nvarchar(400)) AS
BEGIN
    IF OBJECT_ID('tempdb..#result') IS NULL
        CREATE TABLE #result (amount int);
    INSERT INTO #result EXEC (@cmd);
END
GO
CREATE PROCEDURE dbo.Owner (@cmd nvarchar(400)) AS
BEGIN
    CREATE TABLE #result (amount int);
    INSERT INTO #result (amount) SELECT amount FROM dbo.Src;
    EXEC dbo.Helper @cmd;
    INSERT INTO dbo.Out_conditional (amount) SELECT amount FROM #result;
END
GO
