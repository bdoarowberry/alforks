CREATE TABLE dbo.target (k int, c int);
CREATE TABLE dbo.feed (k int, v int, w int);
GO
CREATE PROCEDURE dbo.load_target AS
BEGIN
    UPDATE t SET c *= f.v - f.w FROM dbo.target t JOIN dbo.feed f ON f.k = t.k;
END
GO
