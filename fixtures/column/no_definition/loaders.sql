-- dbo.Feed has no CREATE TABLE in the scanned files; its loader names its columns.
CREATE TABLE dbo.Raw (id int, col_a int, col_b int);
CREATE TABLE dbo.Out_copy (col_a int);
CREATE TABLE dbo.Out_cte (col_b int);
CREATE TABLE dbo.Out_missing (col_z int);
GO
CREATE PROCEDURE dbo.load_feed AS
    INSERT INTO dbo.Feed (id, col_a) SELECT id, col_a FROM dbo.Raw;
GO
CREATE PROCEDURE dbo.fix_feed AS
    UPDATE dbo.Feed SET col_b = 0 WHERE col_b IS NULL;
GO
CREATE PROCEDURE dbo.load_copy AS
BEGIN
    SELECT TOP (0) * INTO #stage FROM dbo.Feed;
    INSERT INTO #stage (col_a) SELECT col_a FROM dbo.Raw;
    WITH c AS (SELECT *, ROW_NUMBER() OVER (ORDER BY col_a) AS rn FROM #stage)
    INSERT INTO dbo.Out_copy (col_a) SELECT col_a FROM c WHERE rn = 1;
END
GO
CREATE PROCEDURE dbo.load_cte AS
BEGIN
    WITH c AS (SELECT * FROM dbo.Feed)
    INSERT INTO dbo.Out_cte (col_b) SELECT col_b FROM c;
END
GO
-- col_z is named by no writer: it stays a gap.
CREATE PROCEDURE dbo.load_missing AS
BEGIN
    WITH c AS (SELECT * FROM dbo.Feed)
    INSERT INTO dbo.Out_missing (col_z) SELECT col_z FROM c;
END
GO
