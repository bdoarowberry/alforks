CREATE TABLE dbo.T (id int NOT NULL IDENTITY, col_a int, col_b int);
GO
CREATE PROCEDURE dbo.load AS INSERT INTO dbo.T VALUES (10, 20);
GO
