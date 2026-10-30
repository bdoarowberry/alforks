CREATE PROCEDURE demo.load_from_remote AS
BEGIN
    INSERT INTO demo.output (id, result_value)
    SELECT r.id, r.amount
    FROM LNK.remote_db.dbo.ext_table AS r;
END
GO
