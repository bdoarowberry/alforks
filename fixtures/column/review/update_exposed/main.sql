CREATE PROCEDURE demo.upd_exposed AS
UPDATE output SET result_value = s.input_a
FROM demo.output
JOIN demo.source s ON s.id = output.id;
GO
