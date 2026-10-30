CREATE TABLE schema1.stage1 (key_col int, target_col int);
GO
CREATE TABLE schema1.side_table (key_col int, other_col int);
GO
CREATE PROCEDURE schema1.load_stage2 AS
BEGIN
    WITH c AS (
      SELECT key_col, MAX(other_col) AS extra_col
      FROM schema1.side_table GROUP BY key_col
    )
    SELECT s.*, c.extra_col
    INTO schema1.stage2
    FROM schema1.stage1 AS s
    LEFT JOIN c ON c.key_col = s.key_col;
END
GO
