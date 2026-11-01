CREATE PROCEDURE dbo.load_result AS
BEGIN
    CREATE TABLE #working (group_id int, col_a int);
    INSERT INTO #working (group_id, col_a)
    SELECT group_id, col_a FROM dbo.Feed;

    SELECT group_id, SUM(col_a) AS total_a
    INTO #totals
    FROM #working
    GROUP BY group_id;

    UPDATE w
    SET col_a = t.total_a
    FROM #working AS w
    JOIN #totals AS t ON t.group_id = w.group_id;

    INSERT INTO dbo.Result (group_id, col_a)
    SELECT group_id, col_a FROM #working;
END
