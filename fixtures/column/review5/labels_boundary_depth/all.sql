CREATE TABLE stage.a (id int, c varchar(10));
GO
CREATE TABLE dbo.b (id int, c varchar(10));
GO
CREATE TABLE dbo.raw (id int, c varchar(10));
GO
CREATE TABLE dbo.out (id int, x varchar(20));
GO
CREATE PROCEDURE dbo.load_b AS
INSERT INTO dbo.b (id, c) SELECT r.id, r.c FROM dbo.raw r;
GO
CREATE PROCEDURE dbo.load_out AS
INSERT INTO dbo.out (id, x) SELECT a.id, a.c + b.c FROM stage.a a JOIN dbo.b b ON b.id = a.id;
GO
