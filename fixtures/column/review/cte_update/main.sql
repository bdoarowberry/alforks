CREATE PROCEDURE demo.fix_output AS
WITH c AS (
    SELECT o.result_value, s.input_b
    FROM demo.output o JOIN demo.source s ON s.id = o.id
)
UPDATE c SET result_value = input_b;
GO
