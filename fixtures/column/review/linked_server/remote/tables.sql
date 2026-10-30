CREATE TABLE remote_db.dbo.ext_table (id int, amount int);
GO
CREATE PROCEDURE remote_db.dbo.load_ext AS
INSERT INTO remote_db.dbo.ext_table (id, amount) SELECT s.id, s.raw_amount FROM remote_db.dbo.staging s;
GO
