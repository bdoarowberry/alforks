CREATE TABLE dbo.target (k int, c int);
CREATE TABLE dbo.feed (k int, v int);
GO
CREATE VIEW dbo.target_v AS SELECT k, c FROM dbo.target;
GO
