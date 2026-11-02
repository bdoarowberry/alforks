CREATE TABLE dbo.Tgt (k int NOT NULL, v int NULL);
GO
CREATE PROCEDURE dbo.load_tgt
AS
BEGIN
    UPDATE t SET v = s.x
    FROM dbo.Tgt t
    JOIN src_db.dbo.Src s ON s.k = t.k
    WHERE s.flag = 1;
END
