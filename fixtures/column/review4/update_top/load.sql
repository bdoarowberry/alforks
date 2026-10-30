CREATE TABLE dbo.target (k int, c int);
CREATE TABLE dbo.feed (k int, v int);
GO
CREATE PROCEDURE dbo.load_target AS
BEGIN
    UPDATE TOP (100) t SET c = f.v FROM dbo.target t JOIN dbo.feed f ON f.k = t.k;
    INSERT TOP (10) INTO dbo.target (k, c) SELECT f.k, f.v * 2 FROM dbo.feed f;
END
GO
