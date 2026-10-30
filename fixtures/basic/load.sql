CREATE PROCEDURE demo.load_result AS
BEGIN
    SELECT
        r.id,
        CASE WHEN r.input_a IS NOT NULL THEN r.input_a ELSE r.input_b END AS chosen_value
    INTO #work
    FROM demo.raw_input AS r
    JOIN demo.lookup_key AS k ON r.id = k.id
    WHERE r.is_active = 1;

    INSERT INTO demo.final_result (id, result_value)
    SELECT id, chosen_value
    FROM #work;
END;
