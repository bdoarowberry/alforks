CREATE PROCEDURE demo.load_u AS
INSERT INTO demo.output (id, result_value) SELECT s.id, s.input_a FROM demo.source s;
GO
