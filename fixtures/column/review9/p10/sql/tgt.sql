CREATE TABLE dbo.Tgt (k int NOT NULL, v int NULL);
CREATE TABLE dbo.RealSrc (k int NOT NULL, x int NULL);
GO
CREATE SYNONYM dbo.SrcSyn FOR dbo.RealSrc;
GO
CREATE PROCEDURE dbo.load_tgt AS
BEGIN
    INSERT dbo.Tgt (k, v) SELECT s.k, s.x FROM dbo.SrcSyn s;
END
GO
CREATE PROCEDURE dbo.fill_real AS
BEGIN
    UPDATE r SET x = o.y FROM dbo.RealSrc r JOIN other_db.dbo.Origin o ON o.k = r.k;
END
