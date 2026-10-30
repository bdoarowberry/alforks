CREATE PROCEDURE dbo.load @p int AS
INSERT INTO db1.dbo.out1 (id, val) VALUES (2, @p * 100);
GO
