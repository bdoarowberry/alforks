CREATE TABLE dbo.SourceTable (col_a int, col_b int);
CREATE TABLE dbo.TargetTable (col_a int, col_b int);
GO
CREATE PROCEDURE dbo.load_target AS
BEGIN
    SELECT * INTO #Result FROM dbo.TargetTable WHERE 1=2;
    INSERT INTO #Result (col_a) SELECT s.col_a FROM dbo.SourceTable AS s;
    INSERT INTO dbo.TargetTable (col_a) SELECT r.col_a FROM #Result AS r;
END;
GO
CREATE PROCEDURE dbo.load_top0 AS
BEGIN
    SELECT TOP (0) col_b INTO #Shape FROM dbo.TargetTable;
    INSERT INTO #Shape (col_b) SELECT s.col_b FROM dbo.SourceTable AS s WHERE 1 = 1 AND s.col_b > 0;
    INSERT INTO dbo.TargetTable (col_b) SELECT x.col_b FROM #Shape AS x WHERE NOT (1 = 0);
END;
GO
