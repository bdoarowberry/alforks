-- Result depends on session state, not on its argument.
CREATE FUNCTION dbo.fn_session (@code varchar(10)) RETURNS varchar(50)
AS
BEGIN
    RETURN CAST(SESSION_CONTEXT(N'tenant') AS varchar(50));
END
GO
-- @flag only chooses between two literals: a condition, not a value.
CREATE FUNCTION dbo.fn_cond (@flag int, @code varchar(10)) RETURNS varchar(50)
AS
BEGIN
    IF @flag = 1
        RETURN 'A';
    RETURN 'B';
END
GO
-- Pure in this database; another database has a same-named function that reads data.
CREATE FUNCTION dbo.fn_pure (@code varchar(10)) RETURNS varchar(50)
AS
BEGIN
    RETURN UPPER(@code);
END
GO
CREATE FUNCTION [dbo].[fn_bracket] (@code varchar(10)) RETURNS varchar(50)
WITH SCHEMABINDING
AS
BEGIN
    RETURN LOWER(@code);
END
GO
-- Shadowed: the ALTER later in the file reads a table.
CREATE FUNCTION dbo.fn_shadow (@code varchar(10)) RETURNS varchar(50)
AS
BEGIN
    RETURN @code;
END
GO
-- Parameter names that are prefixes of each other.
CREATE FUNCTION dbo.fn_prefix (@d date, @date date) RETURNS varchar(50)
AS
BEGIN
    RETURN CONVERT(varchar(50), @date, 112);
END
GO
CREATE FUNCTION dbo.fn_decimal (@a decimal(18,2), @b decimal(18, 2) = 0) RETURNS decimal(18,2)
AS
BEGIN
    RETURN @a + @b;
END
GO
CREATE FUNCTION dbo.fn_now (@code varchar(10)) RETURNS datetime
AS
BEGIN
    RETURN GETDATE();
END
GO
