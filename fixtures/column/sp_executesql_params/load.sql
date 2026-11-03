CREATE TABLE dbo.src (id int, amount decimal(18,2), region varchar(10));
CREATE TABLE dbo.tgt (id int, amount decimal(18,2), batch_id int);
CREATE TABLE dbo.tgt_pos (id int, batch_id int);
CREATE TABLE dbo.tgt_out (id int, batch_id int);
CREATE TABLE dbo.tgt_missing (id int, batch_id int);
CREATE TABLE dbo.tgt_str (id int, note varchar(40));
GO
-- Named arguments; the parameter list is a literal.
CREATE PROCEDURE dbo.load_named @region varchar(10), @batch int
AS
BEGIN
    DECLARE @sql nvarchar(max) = N'INSERT INTO dbo.tgt (id, amount, batch_id) SELECT s.id, s.amount, @b FROM dbo.src s WHERE s.region = @r';
    EXEC sp_executesql @sql, N'@r varchar(10), @b int', @r = @region, @b = @batch;
END
GO
CREATE PROCEDURE dbo.run_named AS EXEC dbo.load_named @region = 'EU', @batch = 42;
GO
-- Positional arguments; the parameter list is held in a variable.
CREATE PROCEDURE dbo.load_positional
AS
BEGIN
    DECLARE @params nvarchar(200) = N'@b int';
    EXEC sp_executesql N'INSERT INTO dbo.tgt_pos (id, batch_id) SELECT s.id, @b FROM dbo.src s', @params, 7;
END
GO
-- An OUTPUT parameter: values written back are not traced, so the statement stays a gap.
CREATE PROCEDURE dbo.load_output
AS
BEGIN
    DECLARE @n int;
    EXEC sp_executesql N'INSERT INTO dbo.tgt_out (id, batch_id) SELECT s.id, @b FROM dbo.src s; SELECT @c = 1',
         N'@b int, @c int OUTPUT', @b = 5, @c = @n OUTPUT;
END
GO
-- A declared parameter with no argument: not evaluated.
CREATE PROCEDURE dbo.load_missing
AS
    EXEC sp_executesql N'INSERT INTO dbo.tgt_missing (id, batch_id) SELECT s.id, @b FROM dbo.src s', N'@b int';
GO
-- "@b" inside a string literal of the dynamic SQL is text, not the parameter.
CREATE PROCEDURE dbo.load_string
AS
    EXEC sp_executesql N'INSERT INTO dbo.tgt_str (id, note) SELECT s.id, ''uses @b'' FROM dbo.src s WHERE s.id = @b',
         N'@b int', @b = 3;
GO
