CREATE TABLE demo.source (id int, input_a int);
GO
CREATE TABLE demo.other (id int, v int);
GO
CREATE TABLE demo.output (id int, result_value int);
GO
CREATE PROCEDURE demo.load_ua AS
INSERT INTO demo.output (id, result_value)
SELECT x.id, x.v FROM (SELECT s.id, s.input_a AS v FROM demo.source s) x
UNION ALL
SELECT x.id, x.v FROM (SELECT o.id, o.v FROM demo.other o) x;
GO
