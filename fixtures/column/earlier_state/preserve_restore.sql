CREATE PROCEDURE dbo.load_target AS
BEGIN
    CREATE TABLE #saved (group_id int, col_a int);
    INSERT INTO #saved (group_id, col_a)
    SELECT group_id, col_a FROM dbo.Target WHERE group_id <> 2;

    DELETE FROM dbo.Target WHERE group_id <> 2;
    INSERT INTO dbo.Target (group_id, col_a)
    SELECT group_id, col_a FROM dbo.NewInput;
    INSERT INTO dbo.Target (group_id, col_a)
    SELECT group_id, col_a FROM #saved;
END
