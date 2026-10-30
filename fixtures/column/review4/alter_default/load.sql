CREATE TABLE dbo.target (k int, c int NOT NULL);
GO
ALTER TABLE dbo.target ADD CONSTRAINT df_target_c DEFAULT (42) FOR c;
GO
CREATE TABLE dbo.feed (k int, v int);
GO
CREATE PROCEDURE dbo.load_target AS
    INSERT INTO dbo.target (k) SELECT f.k FROM dbo.feed f;
GO
