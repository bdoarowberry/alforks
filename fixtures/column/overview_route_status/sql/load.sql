-- Invented: the target is loaded by dynamic SQL (covered by an assertion); its asserted input is loaded
-- from a source table by ordinary, verified SQL. Two other source columns are unresolved.
CREATE TABLE dbo.source_a (id int, col int);
CREATE TABLE dbo.staging (id int, col int);
CREATE TABLE dbo.target (id int, col int);
GO
CREATE PROCEDURE dbo.load_staging AS
    INSERT INTO dbo.staging (id, col) SELECT a.id, a.col FROM dbo.source_a a;
GO
CREATE PROCEDURE dbo.load_target @table sysname AS
BEGIN
    DECLARE @sql nvarchar(max) = N'INSERT INTO dbo.target (id, col) SELECT id, col FROM ' + @table;
    EXEC (@sql);
END
GO
