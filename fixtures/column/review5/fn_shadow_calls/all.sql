CREATE TABLE stage.src (id int, code varchar(10));
GO
CREATE TABLE stage.lookup (code varchar(10), label varchar(50));
GO
CREATE TABLE dbo.out (id int, val varchar(50));
GO
CREATE FUNCTION dbo.fn_reads (@code varchar(10)) RETURNS varchar(50)
AS
BEGIN
    RETURN (SELECT TOP 1 l.label FROM stage.lookup l WHERE l.code = @code);
END
GO
-- First version: pure.
CREATE FUNCTION dbo.fn_label (@code varchar(10)) RETURNS varchar(50)
AS
BEGIN
    RETURN UPPER(@code);
END
GO
-- Deployed version: same parameters, now calls a function that reads a table.
ALTER FUNCTION dbo.fn_label (@code varchar(10)) RETURNS varchar(50)
AS
BEGIN
    RETURN dbo.fn_reads(@code);
END
GO
CREATE PROCEDURE dbo.load_out AS
INSERT INTO dbo.out (id, val) SELECT s.id, dbo.fn_label(s.code) FROM stage.src s;
GO
