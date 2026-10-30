CREATE TABLE dbo.out1 (id int, val int);
GO
CREATE PROCEDURE dbo.load @p int AS
INSERT INTO dbo.out1 (id, val) VALUES (1, @p);
GO
