CREATE PROCEDURE demo.merge_output
AS
BEGIN
    MERGE demo.output AS tgt
    USING demo.source AS src
        ON tgt.id = src.id
    WHEN MATCHED AND src.input_a IS NOT NULL THEN
        UPDATE SET tgt.result_value = src.input_a
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (id, result_value)
        VALUES (src.id, src.input_b);
END;
GO
