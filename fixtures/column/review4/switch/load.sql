CREATE TABLE dbo.target (k int, c int);
CREATE TABLE dbo.target_stage (k int, c int);
CREATE TABLE dbo.feed (k int, v int);
GO
CREATE PROCEDURE dbo.load_target AS
BEGIN
    TRUNCATE TABLE dbo.target_stage;
    INSERT INTO dbo.target_stage (k, c) SELECT f.k, f.v FROM dbo.feed f;
    TRUNCATE TABLE dbo.target;
    ALTER TABLE dbo.target_stage SWITCH TO dbo.target;
END
GO
