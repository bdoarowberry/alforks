CREATE TABLE dbo.target (k int, c int);
CREATE TABLE dbo.feed (k int, v int);
GO
CREATE PROCEDURE dbo.load_target
WITH EXECUTE AS OWNER
AS
INSERT INTO dbo.target (k, c) SELECT f.k, f.v FROM dbo.feed f;
GO
