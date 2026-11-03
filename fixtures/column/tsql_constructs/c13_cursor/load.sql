CREATE TABLE dbo.cur_src1 (k int, v varchar(10));
GO
CREATE TABLE dbo.cur_src2 (k int, v varchar(10));
GO
CREATE TABLE dbo.cur_tgt (k int, v varchar(10));
GO
CREATE PROCEDURE dbo.load_cursor AS
BEGIN
    DECLARE @k int, @v varchar(10);
    DECLARE c CURSOR LOCAL FAST_FORWARD FOR
        SELECT v, k FROM dbo.cur_src1;
    OPEN c;
    FETCH NEXT FROM c INTO @v, @k;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        INSERT INTO dbo.cur_tgt (k, v) VALUES (@k, @v);
        FETCH NEXT FROM c INTO @v, @k;
    END
    CLOSE c;
    DEALLOCATE c;
END
GO
CREATE PROCEDURE dbo.load_cursor_union AS
BEGIN
    DECLARE @k int, @v varchar(10);
    DECLARE c2 CURSOR FOR
        SELECT k, v FROM dbo.cur_src1
        UNION ALL
        SELECT k, v FROM dbo.cur_src2;
    OPEN c2;
    FETCH NEXT FROM c2 INTO @k, @v;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        INSERT INTO dbo.cur_tgt (k, v) VALUES (@k, @v);
        FETCH NEXT FROM c2 INTO @k, @v;
    END
    CLOSE c2;
    DEALLOCATE c2;
END
GO
