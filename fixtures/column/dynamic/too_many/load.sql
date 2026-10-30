CREATE PROCEDURE demo.load_many
    @p1 int, @p2 int, @p3 int, @p4 int, @p5 int
AS
BEGIN
    DECLARE @sql nvarchar(max);
    DECLARE @a nvarchar(10), @b nvarchar(10), @c nvarchar(10), @d nvarchar(10), @e nvarchar(10);

    CREATE TABLE #stage (id int NOT NULL, staged_value int NULL);

    IF @p1 = 1 SET @a = N'1' ELSE SET @a = N'2';
    IF @p2 = 1 SET @b = N'1' ELSE SET @b = N'2';
    IF @p3 = 1 SET @c = N'1' ELSE SET @c = N'2';
    IF @p4 = 1 SET @d = N'1' ELSE SET @d = N'2';
    IF @p5 = 1 SET @e = N'1' ELSE SET @e = N'2';
    SET @sql = N'INSERT INTO #stage (id, staged_value) SELECT id, input_a + ' + @a + @b + @c + @d + @e + N' FROM demo.source';
    EXEC (@sql);

    INSERT INTO demo.output (id, result_value)
    SELECT st.id, st.staged_value
    FROM #stage AS st;
END;
GO
