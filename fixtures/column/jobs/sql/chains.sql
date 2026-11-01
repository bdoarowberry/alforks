CREATE TABLE dbo.Target2 (id int, col_a int);
CREATE TABLE dbo.Target3 (id int, col_a int);
CREATE TABLE dbo.Target4 (id int, col_a int);
GO
CREATE PROCEDURE dbo.chain_load AS INSERT INTO dbo.Target2 (id, col_a) SELECT id, col_a FROM dbo.Feed;
GO
CREATE PROCEDURE dbo.chain_fix AS UPDATE t SET col_a = f.col_a FROM dbo.Target2 AS t JOIN dbo.Fix AS f ON f.id = t.id;
GO
CREATE PROCEDURE dbo.ov_load AS INSERT INTO dbo.Target3 (id, col_a) SELECT id, col_a FROM dbo.Feed;
GO
CREATE PROCEDURE dbo.ov_fix AS UPDATE t SET col_a = f.col_a FROM dbo.Target3 AS t JOIN dbo.Fix AS f ON f.id = t.id;
GO
CREATE PROCEDURE dbo.manual_load AS INSERT INTO dbo.Target4 (id, col_a) SELECT id, col_a FROM dbo.Feed;
GO
