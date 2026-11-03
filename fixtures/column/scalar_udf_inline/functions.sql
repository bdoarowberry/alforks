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
-- Review cases (invented). A later ALTER into a multi-statement body: which body is deployed is unknown.
CREATE FUNCTION dbo.fn_altered (@code varchar(4))
RETURNS varchar(40)
AS
BEGIN
    RETURN (SELECT region_name FROM dbo.region WHERE region_code = @code);
END;
GO
ALTER FUNCTION dbo.fn_altered (@code varchar(4))
RETURNS varchar(40)
AS
BEGIN
    DECLARE @n varchar(40);
    SELECT @n = country_name FROM dbo.country WHERE region_code = @code;
    RETURN @n;
END;
GO
-- Same body, parameters in another order: which argument meets which column depends on the version.
CREATE FUNCTION dbo.fn_swap (@x varchar(4), @y char(3))
RETURNS varchar(40)
AS
BEGIN
    RETURN (SELECT region_name FROM dbo.region WHERE region_code = @x AND region_name = @y);
END;
GO
ALTER FUNCTION dbo.fn_swap (@y char(3), @x varchar(4))
RETURNS varchar(40)
AS
BEGIN
    RETURN (SELECT region_name FROM dbo.region WHERE region_code = @x AND region_name = @y);
END;
GO
-- An inline TVF later altered into a multi-statement function.
CREATE FUNCTION dbo.tvf_altered (@currency char(3))
RETURNS TABLE
AS
RETURN (SELECT rate FROM dbo.fx_rate WHERE currency = @currency)
GO
ALTER FUNCTION dbo.tvf_altered (@currency char(3))
RETURNS @out TABLE (rate decimal(18,6))
AS
BEGIN
    INSERT INTO @out (rate) SELECT rate * 2 FROM dbo.fx_rate WHERE currency = @currency;
    RETURN;
END;
GO
-- Review cases (invented). Inline TVFs read inside inlined bodies: each call keeps its own nodes.
CREATE FUNCTION dbo.tvf_a (@p int) RETURNS TABLE AS RETURN (SELECT d.v FROM (SELECT a_val AS v FROM dbo.lk_a WHERE k = @p) d)
GO
CREATE FUNCTION dbo.tvf_b (@p int) RETURNS TABLE AS RETURN (SELECT d.v FROM (SELECT b_val AS v FROM dbo.lk_b WHERE k = @p) d)
GO
CREATE FUNCTION dbo.fn_via_a (@p int) RETURNS int AS BEGIN RETURN (SELECT t.v FROM dbo.tvf_a(@p) t); END;
GO
CREATE FUNCTION dbo.fn_via_b (@p int) RETURNS int AS BEGIN RETURN (SELECT t.v FROM dbo.tvf_b(@p) t); END;
GO
CREATE FUNCTION dbo.fn_val (@p int) RETURNS int AS BEGIN RETURN (SELECT a_val FROM dbo.lk_a WHERE k = @p); END;
GO
