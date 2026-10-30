CREATE TABLE dbo.target (k int, c int);
CREATE TABLE dbo.feed (k int, v int);
GO
CREATE PROCEDURE dbo.load_target @stmt nvarchar(max) AS
BEGIN
    INSERT INTO dbo.target (k, c) SELECT f.k, f.v FROM dbo.feed f;
    EXEC (@stmt);
    IF @@ERROR <> 0 RAISERROR('Step failed while copying rows into the archive', 16, 1);
END
GO
