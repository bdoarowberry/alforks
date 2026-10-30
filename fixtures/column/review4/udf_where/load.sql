CREATE TABLE dbo.target (k int, c int);
CREATE TABLE dbo.feed (k int, v int);
GO
CREATE PROCEDURE dbo.load_target AS
BEGIN
    INSERT INTO dbo.target (k, c) SELECT f.k, f.v FROM dbo.feed f WHERE dbo.is_active(f.k) = 1;
END
GO
