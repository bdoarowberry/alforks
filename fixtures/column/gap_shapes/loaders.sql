-- Empty schema copy of a table with two differing definitions, then a CTE with SELECT *.
CREATE PROCEDURE dbo.load_cte AS
BEGIN
    SELECT TOP (0) * INTO #stage FROM dbo.Twice;
    INSERT INTO #stage (col_a) SELECT col_a FROM dbo.SourceTable;
    WITH c AS (SELECT *, ROW_NUMBER() OVER (ORDER BY col_a) AS rn FROM #stage)
    INSERT INTO dbo.Out_cte (col_a) SELECT col_a FROM c WHERE rn = 1;
END
GO
-- Qualified * copy, then a positional INSERT.
CREATE PROCEDURE dbo.load_star AS
BEGIN
    SELECT TOP (0) s.* INTO #copy FROM dbo.SourceTable AS s;
    INSERT INTO #copy SELECT id, col_a, flag FROM dbo.SourceTable;
    INSERT INTO dbo.Out_star (col_a) SELECT col_a FROM #copy;
END
GO
-- The copied table has an identity column: position matching is not safe.
CREATE PROCEDURE dbo.load_identity AS
BEGIN
    SELECT TOP (0) * INTO #ident FROM dbo.WithIdentity;
    INSERT INTO #ident SELECT col_a FROM dbo.SourceTable;
    INSERT INTO dbo.Out_identity (col_a) SELECT col_a FROM #ident;
END
GO
CREATE PROCEDURE dbo.load_added AS
    INSERT INTO dbo.Out_added (col_new) SELECT col_new FROM dbo.Added;
GO
-- The column is added at run time, so no scanned definition lists it.
CREATE PROCEDURE dbo.add_late AS
    EXEC('ALTER TABLE dbo.Late ADD col_new int');
GO
CREATE PROCEDURE dbo.load_late AS
    INSERT INTO dbo.Out_late (col_new) SELECT col_new FROM dbo.Late;
GO
CREATE PROCEDURE dbo.load_join AS
    INSERT INTO dbo.Out_join (col_a) SELECT col_a FROM dbo.LeftTable l JOIN dbo.RightTable r ON l.id = r.id;
GO
CREATE PROCEDURE dbo.GetAmounts AS
BEGIN
    CREATE TABLE #work (amount decimal(12, 2));
    INSERT INTO #work (amount) SELECT col_a FROM dbo.SourceTable;
    SELECT amount FROM #work;
END
GO
CREATE PROCEDURE dbo.Report AS
BEGIN
    CREATE TABLE #result (amount decimal(12, 2));
    INSERT INTO #result EXEC dbo.GetAmounts;
    INSERT INTO dbo.Out_exec (amount) SELECT amount FROM #result;
END
GO
-- Same #temp name, own CREATE, EXEC of a string: must not write Report's #result.
CREATE PROCEDURE dbo.Archive AS
BEGIN
    DECLARE @command nvarchar(400) = N'dir';
    CREATE TABLE #result (message nvarchar(100));
    INSERT INTO #result EXEC(@command);
END
GO
CREATE PROCEDURE dbo.TwoResults AS
BEGIN
    SELECT col_a AS amount FROM dbo.SourceTable;
    SELECT flag AS amount FROM dbo.SourceTable;
END
GO
CREATE PROCEDURE dbo.ReportTwo AS
BEGIN
    CREATE TABLE #two (amount decimal(12, 2));
    INSERT INTO #two (amount) EXEC dbo.TwoResults;
    INSERT INTO dbo.Out_two (amount) SELECT amount FROM #two;
END
GO
CREATE FUNCTION dbo.TableFunction (@id int) RETURNS TABLE AS
RETURN (SELECT s.col_a FROM dbo.SourceTable AS s WHERE s.id = @id);
GO
CREATE PROCEDURE dbo.load_tvf AS
    INSERT INTO dbo.Out_tvf (col_a)
    SELECT x.col_a FROM dbo.Parent AS p
    CROSS APPLY (SELECT * FROM dbo.TableFunction(p.id)) AS x;
GO
CREATE PROCEDURE dbo.load_backup AS
    INSERT INTO dbo.Out_backup (col_a) SELECT col_a FROM dbo.Out_backup_Saved;
GO
-- Identity passes through a derived table into the copy: position matching is not safe.
CREATE PROCEDURE dbo.load_identity_derived AS
BEGIN
    SELECT TOP (0) * INTO #ident2 FROM (SELECT id, col_a FROM dbo.WithIdentity) AS d;
    INSERT INTO #ident2 SELECT col_a FROM dbo.SourceTable;
    INSERT INTO dbo.Out_identity2 (col_a) SELECT col_a FROM #ident2;
END
