USE db1
GO
CREATE TABLE dbo.src (id int, a int);
GO
CREATE TABLE dbo.out1 (id int, val int);
GO
CREATE PROCEDURE dbo.load @p int AS
INSERT INTO dbo.out1 (id, val) SELECT s.id, @p FROM dbo.src s;
GO
USE db2
GO
CREATE TABLE dbo.src (id int, a int);
GO
CREATE PROCEDURE dbo.load @p int AS
INSERT INTO db1.dbo.out1 (id, val) SELECT s.id, s.a + @p FROM dbo.src s;
GO
