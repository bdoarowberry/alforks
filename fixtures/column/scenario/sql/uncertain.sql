-- Invented: parameters changed in the procedure, conditions the values cannot settle, values passed on.
CREATE TABLE dbo.src_a (v int);
CREATE TABLE dbo.src_b (v int);
CREATE TABLE dbo.config (flag bit);
CREATE TABLE dbo.out_changed (v int);
CREATE TABLE dbo.out_selected (v int);
CREATE TABLE dbo.out_exists (v int);
CREATE TABLE dbo.out_child (v int);
CREATE TABLE dbo.out_output (v int);
CREATE TABLE dbo.out_goto (v int);
CREATE TABLE dbo.out_param (v int);
GO
-- SET under a condition the values settle: the new value is followed.
CREATE PROC dbo.p_changed @mode bit AS
BEGIN
    IF @mode = 1 SET @mode = 0;
    IF @mode = 0
        INSERT dbo.out_changed (v) SELECT v FROM dbo.src_a;
    ELSE
        INSERT dbo.out_changed (v) SELECT v FROM dbo.src_b;
END
GO
-- Assigned from a query: unknown from there on.
CREATE PROC dbo.p_selected @mode bit AS
BEGIN
    SELECT @mode = c.flag FROM dbo.config c;
    IF @mode = 0
        INSERT dbo.out_selected (v) SELECT v FROM dbo.src_a;
    ELSE
        INSERT dbo.out_selected (v) SELECT v FROM dbo.src_b;
END
GO
-- A condition that also reads a table.
CREATE PROC dbo.p_exists @mode bit AS
BEGIN
    IF @mode = 1 AND EXISTS (SELECT 1 FROM dbo.config WHERE flag = 1)
        INSERT dbo.out_exists (v) SELECT v FROM dbo.src_a;
    ELSE
        INSERT dbo.out_exists (v) SELECT v FROM dbo.src_b;
END
GO
-- The value is passed to another procedure: its branches are not constrained.
CREATE PROC dbo.p_child @m bit AS
BEGIN
    IF @m = 0
        INSERT dbo.out_child (v) SELECT v FROM dbo.src_a;
    ELSE
        INSERT dbo.out_child (v) SELECT v FROM dbo.src_b;
END
GO
CREATE PROC dbo.p_parent @mode bit AS
    EXEC dbo.p_child @m = @mode;
GO
-- Passed as an OUTPUT argument: the callee may change it.
CREATE PROC dbo.p_flip @x bit OUTPUT AS SET @x = 1 - @x;
GO
CREATE PROC dbo.p_output @mode bit AS
BEGIN
    EXEC dbo.p_flip @x = @mode OUTPUT;
    IF @mode = 0
        INSERT dbo.out_output (v) SELECT v FROM dbo.src_a;
    ELSE
        INSERT dbo.out_output (v) SELECT v FROM dbo.src_b;
END
GO
-- GOTO: jumps are not followed, so nothing is excluded.
CREATE PROC dbo.p_goto @mode bit AS
BEGIN
    IF @mode = 1 GOTO done;
    INSERT dbo.out_goto (v) SELECT v FROM dbo.src_a;
done:
    INSERT dbo.out_goto (v) SELECT v FROM dbo.src_b;
END
GO
-- Calls in an excluded branch: their parameter bindings drop; the callee's own writer stays.
CREATE PROC dbo.p_store @m int AS INSERT dbo.out_param (v) SELECT @m;
GO
CREATE PROC dbo.p_caller @mode bit AS
BEGIN
    IF @mode = 0
        EXEC dbo.p_store @m = 5;
    ELSE
        EXEC dbo.p_store @m = 7;
END
GO
