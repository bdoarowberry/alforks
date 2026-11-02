CREATE TABLE dbo.Tgt (k int NOT NULL, v int NULL);
GO
CREATE PROCEDURE dbo.load_tgt AS
BEGIN
    INSERT dbo.Tgt (k, v) SELECT s.k, s.x FROM [RemoteSrv].src_db.dbo.Src s;
END
