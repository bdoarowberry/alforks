CREATE PROCEDURE demo.load_two AS
BEGIN
    INSERT INTO demo.output (id, result_value) SELECT s.id, s.input_a FROM demo.source s;
    INSERT INTO demo.output (id, other_value) SELECT o.id, o.v FROM demo.other o;
END
GO
