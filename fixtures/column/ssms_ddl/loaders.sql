CREATE PROCEDURE dbo.load_cte AS
BEGIN
    SELECT TOP (0) * INTO #stage FROM dbo.SourceTable;
    INSERT INTO #stage (col_a) SELECT col_a FROM dbo.Feed;
    WITH c AS (SELECT *, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM #stage)
    INSERT INTO dbo.Out_cte (col_a) SELECT col_a FROM c WHERE rn = 1;
END
GO
CREATE PROCEDURE dbo.load_target AS
    INSERT INTO dbo.TargetTable SELECT col_a, col_b, secret FROM dbo.Feed;
GO
CREATE PROCEDURE dbo.load_indexed AS
    INSERT INTO dbo.Indexed SELECT col_b, id FROM dbo.Feed;
GO
CREATE PROCEDURE dbo.load_timed AS
    INSERT INTO dbo.Timed SELECT id, col_b FROM dbo.Feed;
GO
