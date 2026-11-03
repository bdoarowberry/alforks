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
-- Review cases (invented). EXEC argument names are never rewritten: @id = @id passes the caller's @key.
CREATE TABLE dbo.t_arg (id int, x int);
CREATE TABLE dbo.t_reassign (id int, x int);
CREATE TABLE dbo.t_clash (id int, x int);
CREATE TABLE dbo.t_word (id int, x varchar(10));
CREATE TABLE dbo.t_neg (id int, x int);
CREATE TABLE dbo.t_nested (id int, x int);
CREATE TABLE dbo.t_default (id int, x int);
CREATE TABLE dbo.other (id int, y int);
GO
CREATE PROCEDURE dbo.load_arg @id int = 0 AS INSERT INTO dbo.t_arg (id, x) SELECT s.id, @id FROM dbo.src s;
GO
CREATE PROCEDURE dbo.run_arg @key int AS EXEC sp_executesql N'EXEC dbo.load_arg @id = @id', N'@id int', @id = @key;
GO
CREATE PROCEDURE dbo.start_arg AS EXEC dbo.run_arg @key = 11;
GO
-- The parameter is reassigned inside the batch: its value there is not the argument.
CREATE PROCEDURE dbo.load_reassign @v int AS
    EXEC sp_executesql N'SELECT @p = MAX(o.y) FROM dbo.other o; INSERT INTO dbo.t_reassign (id, x) SELECT s.id, @p FROM dbo.src s',
         N'@p int', @p = @v;
GO
-- The batch declares its own @v, the name of the caller variable passed as @p.
CREATE PROCEDURE dbo.load_clash @v int AS
    EXEC sp_executesql N'DECLARE @v int; SELECT @v = MAX(o.y) FROM dbo.other o; INSERT INTO dbo.t_clash (id, x) SELECT s.id, @v + @p FROM dbo.src s',
         N'@p int', @p = @v;
GO
-- An unquoted word is a string argument, not a column.
CREATE PROCEDURE dbo.load_word AS
    EXEC sp_executesql N'INSERT INTO dbo.t_word (id, x) SELECT s.id, @r FROM dbo.src s', N'@r varchar(10)', @r = active;
GO
-- A negative argument after a minus sign must not become a -- comment.
CREATE PROCEDURE dbo.load_neg AS
    EXEC sp_executesql N'INSERT INTO dbo.t_neg (id, x) SELECT s.id, s.id-@p + o.y
FROM dbo.src s JOIN dbo.other o ON o.id = s.id', N'@p int', @p = -1;
GO
-- Nested EXEC () runs in its own batch: the caller's @v is not visible there.
CREATE PROCEDURE dbo.load_nested @v int AS
    EXEC sp_executesql N'EXEC (N''INSERT INTO dbo.t_nested (id, x) SELECT s.id, @v FROM dbo.src s'')', N'@p int', @p = @v;
GO
-- DEFAULT takes the declared default of the parameter list.
CREATE PROCEDURE dbo.load_default AS
    EXEC sp_executesql N'INSERT INTO dbo.t_default (id, x) SELECT s.id, @d FROM dbo.src s', N'@d int = 9', @d = DEFAULT;
GO
