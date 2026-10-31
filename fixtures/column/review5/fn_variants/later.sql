-- A later ALTER replaces the body: it now reads a table.
ALTER FUNCTION dbo.fn_shadow (@code varchar(10)) RETURNS varchar(50)
AS
BEGIN
    RETURN (SELECT TOP 1 l.label FROM stage.lookup l WHERE l.code = @code);
END
GO
-- Calls a function that reads data.
CREATE FUNCTION dbo.fn_nested (@code varchar(10)) RETURNS varchar(50)
AS
BEGIN
    RETURN dbo.fn_shadow(@code);
END
GO
