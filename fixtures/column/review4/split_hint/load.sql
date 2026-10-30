CREATE TABLE dbo.target (k int, c int);
CREATE TABLE dbo.feed (k int, v int, s varchar(10));
GO
CREATE PROCEDURE dbo.load_target AS
BEGIN
    UPDATE t
    SET c = f.v
    FROM dbo.feed f
        WITH (NOLOCK)
    JOIN dbo.target t ON t.k = f.k
    WHERE TRY_PARSE(f.s AS int USING 'en-US') > 0;
END
GO
