CREATE TABLE dbo.target (k int, c int);
CREATE TABLE dbo.feed (k int, v int);
GO
CREATE PROCEDURE dbo.load_target AS
BEGIN
    INSERT INTO dbo.target (k, c)
    SELECT f.k, (SELECT MAX(v) FROM dbo.outside_table x WHERE x.k = f.k)
    FROM dbo.feed f;
END
GO
