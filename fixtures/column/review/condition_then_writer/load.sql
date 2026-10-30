USE db1;
GO
CREATE TABLE schema1.stage (id int, source_ref varchar(20), amount int);
GO
CREATE TABLE schema1.destination (id int, reference_no varchar(20), amount int, note varchar(20));
GO
CREATE PROCEDURE schema1.load_destination AS
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema1.destination WHERE reference_no = 'x') INSERT INTO schema1.destination (id, reference_no) SELECT s.id, s.source_ref FROM schema1.stage AS s;
    IF (SELECT COUNT(*) FROM schema1.stage) > 0 UPDATE d SET d.amount = s.amount FROM schema1.destination d JOIN schema1.stage s ON s.id = d.id;
    DECLARE @i int = 0; WHILE @i < 1 BEGIN SET @i = @i + 1; END; DROP TABLE IF EXISTS #scratch; UPDATE schema1.destination SET note = 'done';
END
GO
