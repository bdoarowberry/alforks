CREATE PROCEDURE demo.load_output
AS
BEGIN
    -- INSERT INTO demo.output (id, result_value) SELECT d.id, d.input_b FROM demo.decoy AS d;
    /*
        UPDATE o SET o.result_value = d.input_b
        FROM demo.output AS o INNER JOIN demo.decoy AS d ON d.id = o.id;
    */
    INSERT INTO demo.audit_log (message)
    VALUES ('UPDATE demo.output SET result_value = input_b FROM demo.decoy -- not executed');

    INSERT INTO demo.output (id, result_value)
    SELECT s.id, s.input_a /* not d.input_b */
    FROM demo.source AS s; -- SELECT input_b FROM demo.decoy
END;
GO
