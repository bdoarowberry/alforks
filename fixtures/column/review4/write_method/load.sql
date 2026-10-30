CREATE TABLE dbo.doc (k int, body varchar(max));
CREATE TABLE dbo.feed (k int, chunk varchar(100));
GO
CREATE PROCEDURE dbo.append_chunks AS
BEGIN
    UPDATE d SET body.WRITE(f.chunk, NULL, NULL) FROM dbo.doc d JOIN dbo.feed f ON f.k = d.k;
END
GO
