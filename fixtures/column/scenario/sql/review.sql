-- Invented shapes from the adversarial review of --scenario: each wrongly excluded a statement that runs.
CREATE TABLE dbo.r_src_a (v int);
CREATE TABLE dbo.r_src_b (v int);
CREATE TABLE dbo.r_print (v int);
CREATE TABLE dbo.r_else (v int);
CREATE TABLE dbo.r_case (v int);
CREATE TABLE dbo.r_loop (v int);
CREATE TABLE dbo.r_dialog (v int);
CREATE TABLE dbo.r_trunc (v int);
CREATE TABLE dbo.r_int (v int);
CREATE TABLE dbo.r_setbit (v int);
CREATE TABLE dbo.r_accent (v int);
CREATE TABLE dbo.r_null (v int);
CREATE TABLE dbo.r_word (v int);
CREATE TABLE dbo.r_dyn (v int);
CREATE TABLE dbo.r_cols (v int, w int);
GO
CREATE PROC dbo.r_p_print @dbg bit AS
BEGIN
    IF @dbg = 1 BEGIN PRINT 'debug' END
    INSERT dbo.r_print (v) SELECT v FROM dbo.r_src_a;
END
GO
CREATE PROC dbo.r_p_else @dbg bit AS
BEGIN
    IF @dbg = 1 PRINT 'debug' ELSE INSERT dbo.r_else (v) SELECT v FROM dbo.r_src_a;
END
GO
CREATE PROC dbo.r_p_case @p bit, @q bit AS
BEGIN
    IF @p = 1
    BEGIN
        PRINT CASE WHEN @q = 1 THEN 'a' ELSE 'b'
        END
        RETURN
    END
    INSERT dbo.r_case (v) SELECT v FROM dbo.r_src_a;
END
GO
CREATE PROC dbo.r_p_loop @p int AS
BEGIN
    DECLARE @i int
    SET @i = 0
    IF @p = 5 RETURN
    WHILE @i < 2
    BEGIN
        IF @p = 1 INSERT dbo.r_loop (v) SELECT v FROM dbo.r_src_a
        SET @p = 1
        SET @i = @i + 1
    END
END
GO
CREATE PROC dbo.r_p_dialog @p bit AS
BEGIN
    DECLARE @h uniqueidentifier;
    IF @p = 1
    BEGIN
        BEGIN DIALOG CONVERSATION @h FROM SERVICE s_from TO SERVICE 's_to' ON CONTRACT c_x;
        END CONVERSATION @h;
    END
    INSERT dbo.r_dialog (v) SELECT v FROM dbo.r_src_a;
END
GO
CREATE PROC dbo.r_p_trunc @code varchar(3) AS
BEGIN
    IF @code = 'ABCD'
        INSERT dbo.r_trunc (v) SELECT v FROM dbo.r_src_a;
    ELSE
        INSERT dbo.r_trunc (v) SELECT v FROM dbo.r_src_b;
END
GO
CREATE PROC dbo.r_p_int @n int AS
BEGIN
    IF @n = 1
        INSERT dbo.r_int (v) SELECT v FROM dbo.r_src_a;
    ELSE
        INSERT dbo.r_int (v) SELECT v FROM dbo.r_src_b;
END
GO
CREATE PROC dbo.r_p_setbit @b bit AS
BEGIN
    SET @b = 5;
    IF @b = 1
        INSERT dbo.r_setbit (v) SELECT v FROM dbo.r_src_a;
    ELSE
        INSERT dbo.r_setbit (v) SELECT v FROM dbo.r_src_b;
END
GO
CREATE PROC dbo.r_p_accent @w nvarchar(10) AS
BEGIN
    IF @w = N'e'
        INSERT dbo.r_accent (v) SELECT v FROM dbo.r_src_a;
    ELSE
        INSERT dbo.r_accent (v) SELECT v FROM dbo.r_src_b;
END
GO
CREATE PROC dbo.r_p_null @p int AS
BEGIN
    IF @p = NULL
        INSERT dbo.r_null (v) SELECT v FROM dbo.r_src_a;
    ELSE
        INSERT dbo.r_null (v) SELECT v FROM dbo.r_src_b;
END
GO
CREATE PROC dbo.r_p_word @w varchar(10) AS
BEGIN
    IF @w = 'inf'
        INSERT dbo.r_word (v) SELECT v FROM dbo.r_src_a;
    ELSE
        INSERT dbo.r_word (v) SELECT v FROM dbo.r_src_b;
END
GO
CREATE PROC dbo.r_p_dyn @p int AS
BEGIN
    IF @p = 1
        EXEC ('INSERT dbo.r_dyn (v) SELECT v FROM dbo.r_src_a');
    ELSE
        EXEC sp_executesql N'INSERT dbo.r_dyn (v) SELECT v FROM dbo.r_src_b';
END
GO
CREATE PROC dbo.r_p_cols @p int AS
BEGIN
    IF @p = 1 INSERT dbo.r_cols (v) SELECT v FROM dbo.r_src_a;
    UPDATE dbo.r_cols SET w = 1;
END
GO
