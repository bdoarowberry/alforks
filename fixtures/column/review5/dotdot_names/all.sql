CREATE TABLE dbo.src (id int, code varchar(10));
GO
CREATE TABLE stage.src (id int, code varchar(10));
GO
CREATE TABLE dbo.out (id int, code varchar(10), code2 varchar(10), code3 varchar(10));
GO
-- demo..src means demo.dbo.src (default schema), never stage.src.
CREATE PROCEDURE dbo.load_out AS
INSERT INTO demo..out (id, code) SELECT s.id, s.code FROM demo..src s;
GO
-- Linked-server shape with an empty database part: remote, not local.
CREATE PROCEDURE dbo.load_out2 AS
UPDATE o SET o.code2 = r.code FROM dbo.out o JOIN remotesrv..dbo.src r ON r.id = o.id;
GO
-- Spaces around the dots.
CREATE PROCEDURE dbo.load_out3 AS
UPDATE o SET o.code3 = r.code FROM dbo.out o JOIN demo . . src r ON r.id = o.id;
GO
