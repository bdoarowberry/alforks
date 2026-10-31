CREATE TABLE stage.src (id int, code varchar(10));
GO
CREATE TABLE dbo.out (id int, code varchar(10));
GO
CREATE PROCEDURE dbo.load_out AS
INSERT INTO dbo.out (id, code) SELECT s.id, s.code FROM stage.src s;
GO
USE archive;
GO
CREATE PROCEDURE stage.load_src AS
INSERT INTO stage.src (id, code) SELECT f.id, f.code FROM stage.feed f;
GO
/* old loader:
INSERT INTO stage.src (id, code) SELECT 1, 'x';
*/
