CREATE TABLE dbo.target (k int, c int, txt varchar(20));
CREATE TABLE dbo.feed (k int, v varchar(20));
GO
CREATE PROCEDURE dbo.refresh_target AS
BEGIN
    UPDATE o
    SET c = (SELECT TOP 1 TRY_PARSE(o.v AS int USING 'en-US') FROM dbo.feed o WHERE o.k = 1)
    FROM dbo.target o;
END
GO
