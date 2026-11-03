CREATE PROCEDURE dbo.stage_kv AS
BEGIN
    INSERT INTO dbo.kv_stage (k, v) SELECT f.k, f.v FROM dbo.kv_feed AS f;
END
GO
CREATE PROCEDURE dbo.build_into AS
BEGIN
    SELECT IDENTITY(int, 1, 1) AS row_id, s.k, s.v
    INTO dbo.into_tgt2
    FROM dbo.kv_stage AS s;
END
GO
CREATE TABLE dbo.seq_tgt (id bigint, v varchar(10));
GO
CREATE SEQUENCE dbo.seq_id AS bigint START WITH 1 INCREMENT BY 1;
GO
CREATE PROCEDURE dbo.load_seq AS
BEGIN
    INSERT INTO dbo.seq_tgt (id, v)
    SELECT NEXT VALUE FOR seq_id, s.v FROM dbo.kv_stage AS s;
END
GO
CREATE PROCEDURE dbo.load_seq2 AS
BEGIN
    INSERT INTO dbo.seq_tgt (id, v)
    SELECT NEXT VALUE FOR dbo.seq_id, s.v FROM dbo.kv_stage AS s;
END
GO
