CREATE TABLE stage.src (id int, code varchar(10), [End] int, [Select] int);
GO
CREATE TABLE dbo.out (id int, a varchar(10), b int, c int, d varchar(10), e varchar(10));
GO
CREATE PROCEDURE dbo.load_out @End int, @Update int AS
BEGIN
    DECLARE @Set int = 1;
    CREATE TABLE #Set (id int, code varchar(10));
    INSERT INTO #Set (id, code) SELECT s.id, s.code FROM stage.src s WHERE s.[End] = @End
    IF @Update = 1
    BEGIN
        INSERT INTO dbo.out (id, a, b) SELECT x.id, x.code, CASE WHEN x.id = @End THEN 1 END FROM #Set x
    END
    ELSE
        INSERT INTO dbo.out (id, c) SELECT s.id, s.[Select] FROM stage.src s
    UPDATE o SET o.d = CASE WHEN s.[End] > 0 THEN s.code ELSE 'n' END FROM dbo.out o JOIN stage.src s ON s.id = o.id
    CREATE INDEX ix_end ON stage.src ([End]) INCLUDE ([Select]) UPDATE o SET o.e = s.code FROM dbo.out o JOIN stage.src s ON s.id = o.id
END
GO
EXEC dbo.load_out @End = 1, @Update = 0;
GO
