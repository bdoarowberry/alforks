CREATE TABLE demo.source (id int, input_a int, input_b int, k int);
GO
CREATE TABLE demo.output (id int, result_value int);
GO
CREATE PROCEDURE ref.load_rates AS
INSERT INTO ref.rates (k, rate) SELECT s.k, s.input_a FROM demo.source s;
GO
CREATE PROCEDURE demo.load_cw AS
INSERT INTO demo.output (id, result_value)
SELECT s.id, (SELECT MAX(r.rate) * input_b FROM ref.rates r WHERE r.k = s.k)
FROM demo.source s;
GO
