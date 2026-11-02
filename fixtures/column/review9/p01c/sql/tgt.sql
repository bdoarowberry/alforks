CREATE TABLE dbo.Tgt (k int NOT NULL, v int NULL);
GO
CREATE PROCEDURE dbo.load_tgt @expr nvarchar(200)
AS
BEGIN
    UPDATE t SET v = s.x
    FROM dbo.Tgt t
    JOIN src_db.dbo.Src s ON s.k = t.k
    WHERE s.flag = 1;

    DECLARE @sql nvarchar(max) = N'UPDATE dbo.Tgt SET v = ' + @expr + N' WHERE k > 0';
    EXEC (@sql);
END
