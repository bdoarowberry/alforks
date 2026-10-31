CREATE TABLE stage.src (id int, code varchar(10));
GO
CREATE TABLE dbo.out (id int, val varchar(50));
GO
CREATE FUNCTION dbo.clean (@code varchar(10)) RETURNS varchar(50)
AS EXTERNAL NAME LineageAsm.[Lineage.Functions].Clean;
GO
CREATE PROCEDURE dbo.load_out AS
INSERT INTO dbo.out (id, val) SELECT s.id, dbo.clean(s.code) FROM stage.src s;
GO
