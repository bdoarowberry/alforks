CREATE TABLE stage.mid (id int, code varchar(10));
GO
CREATE TABLE dbo.out (id int, code_note varchar(60));
GO
-- One input reaches the requested boundary (stage.*), the other is a parameter no scanned EXEC passes.
CREATE PROCEDURE dbo.load_out @note varchar(50) AS
INSERT INTO dbo.out (id, code_note) SELECT m.id, m.code + @note FROM stage.mid m;
GO
