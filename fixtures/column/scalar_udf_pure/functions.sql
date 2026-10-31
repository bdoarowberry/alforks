CREATE FUNCTION dbo.fn_date_key (@d date, @unused int = 0)
RETURNS int
AS
BEGIN
    DECLARE @k int;
    IF @d IS NULL RETURN NULL;
    SET @k = YEAR(@d) * 10000 + MONTH(@d) * 100 + DAY(@d);
    RETURN @k;
END;
GO
CREATE FUNCTION dbo.fn_wrap (@d date)
RETURNS int
AS
BEGIN
    RETURN dbo.fn_date_key(@d, 1) + 0;
END;
GO
CREATE FUNCTION dbo.fn_lookup (@id int)
RETURNS varchar(20)
AS
BEGIN
    RETURN (SELECT TOP 1 label FROM demo.labels WHERE id = @id);
END;
GO
