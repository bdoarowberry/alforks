CREATE TABLE dbo.target (k int, c int, d int);
CREATE TABLE dbo.feed (k int, v int, w int);
GO
CREATE PROCEDURE dbo.load_target AS
BEGIN
    DECLARE @t int, @__dt int;
    SELECT @t = MAX(f.v) FROM dbo.feed f;
    SELECT @__dt = MAX(f.w) FROM dbo.feed f;
    INSERT INTO dbo.target (k, c, d) VALUES (1, @t, @__dt);
END
GO
