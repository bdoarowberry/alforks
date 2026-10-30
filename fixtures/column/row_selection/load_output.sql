CREATE PROCEDURE demo.load_filtered
AS
BEGIN
    INSERT INTO demo.output (id, result_value)
    SELECT s.id, s.input_a
    FROM demo.source AS s
    INNER JOIN demo.lookup AS k
        ON k.id = s.id
       AND k.region_code <> 'XX'
    WHERE s.is_active = 1;
END;
GO
