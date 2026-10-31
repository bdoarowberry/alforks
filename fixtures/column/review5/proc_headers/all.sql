CREATE TABLE stage.src (id int, code varchar(10));
GO
CREATE TABLE dbo.out (id int, a varchar(10), b varchar(10), c varchar(10), d varchar(10));
GO
CREATE PROCEDURE dbo.load_a WITH RECOMPILE, EXECUTE AS OWNER AS
INSERT INTO dbo.out (id, a) SELECT s.id, s.code FROM stage.src s;
GO
CREATE PROCEDURE dbo.load_b WITH EXECUTE AS 'loader', RECOMPILE AS
INSERT INTO dbo.out (id, b) SELECT s.id, s.code FROM stage.src s;
GO
CREATE PROCEDURE dbo.load_c
WITH NATIVE_COMPILATION, SCHEMABINDING, EXECUTE AS OWNER
AS BEGIN ATOMIC WITH (TRANSACTION ISOLATION LEVEL = SNAPSHOT, LANGUAGE = N'us_english')
    INSERT INTO dbo.out (id, c) SELECT s.id, s.code FROM stage.src s;
END
GO
CREATE PROCEDURE dbo.load_d WITH ENCRYPTION AS
INSERT INTO dbo.out (id, d) SELECT s.id, s.code FROM stage.src s;
GO
