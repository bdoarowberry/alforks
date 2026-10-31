CREATE TABLE stage.raw (id int, code varchar(10));
GO
CREATE TABLE stage.mid (id int, code varchar(10));
GO
CREATE TABLE dbo.out (id int, code varchar(10), note varchar(50));
GO
CREATE PROCEDURE stage.load_mid AS
INSERT INTO stage.mid (id, code) SELECT r.id, r.code FROM stage.raw r;
GO
-- @note comes from a caller that is not in the scanned files.
CREATE PROCEDURE dbo.load_out @note varchar(50) AS
INSERT INTO dbo.out (id, code, note) SELECT m.id, m.code, @note FROM stage.mid m;
GO
