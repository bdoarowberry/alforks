-- Invented shapes. One RETURN of a query: inlined.
CREATE FUNCTION dbo.fn_region_name (@code varchar(4))
RETURNS varchar(40)
AS
BEGIN
    RETURN (SELECT r.region_name FROM dbo.region r WHERE r.region_code = @code);
END;
GO
-- No BEGIN/END, a declared default, and an unqualified column that the caller also has.
CREATE FUNCTION dbo.fn_rate (@currency char(3), @kind varchar(8) = 'SPOT')
RETURNS decimal(18,6)
AS
RETURN (SELECT rate FROM dbo.fx_rate WHERE currency = @currency AND rate_kind = @kind)
GO
-- A column the body's own source does not have: it must not bind to the caller's source.
CREATE FUNCTION dbo.fn_wrong (@id int)
RETURNS int
AS
BEGIN
    RETURN (SELECT amount FROM dbo.region WHERE region_code = @id);
END;
GO
-- Several statements: stays opaque.
CREATE FUNCTION dbo.fn_multi (@code varchar(4))
RETURNS varchar(40)
AS
BEGIN
    DECLARE @n varchar(40);
    SELECT @n = region_name FROM dbo.region WHERE region_code = @code;
    RETURN @n;
END;
GO
-- No declared default for the parameter passed DEFAULT: stays opaque.
CREATE FUNCTION dbo.fn_nodefault (@code varchar(4))
RETURNS varchar(40)
AS
BEGIN
    RETURN (SELECT region_name FROM dbo.region WHERE region_code = @code);
END;
GO
-- Calls itself: never inlined into itself.
CREATE FUNCTION dbo.fn_loop (@n int)
RETURNS int
AS
BEGIN
    RETURN (SELECT dbo.fn_loop(@n - 1) + r.weight FROM dbo.region r WHERE r.region_code = @n);
END;
GO
-- Calls another inlined function with its own parameter.
CREATE FUNCTION dbo.fn_outer (@code varchar(4))
RETURNS varchar(60)
AS
BEGIN
    RETURN (SELECT dbo.fn_region_name(@code) + ' / ' + c.country_name FROM dbo.country c WHERE c.region_code = @code);
END;
GO
-- Two different definitions: which one is deployed is unknown.
CREATE FUNCTION dbo.fn_twice (@code varchar(4))
RETURNS varchar(40)
AS
BEGIN
    RETURN (SELECT region_name FROM dbo.region WHERE region_code = @code);
END;
GO
ALTER FUNCTION dbo.fn_twice (@code varchar(4))
RETURNS varchar(40)
AS
BEGIN
    RETURN (SELECT country_name FROM dbo.country WHERE region_code = @code);
END;
GO
-- Inline table-valued function with a declared default.
CREATE FUNCTION dbo.tvf_rates (@currency char(3), @kind varchar(8) = 'SPOT')
RETURNS TABLE
AS
RETURN (SELECT rate, currency FROM dbo.fx_rate WHERE currency = @currency AND rate_kind = @kind)
GO
