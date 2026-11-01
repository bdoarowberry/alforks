CREATE TABLE dbo.Feed (id int, col_a int, col_b int);
CREATE TABLE dbo.Fix (id int, col_a int, col_b int);
CREATE TABLE dbo.Target (id int, col_a int, col_b int, col_c int, col_d int);
GO
CREATE PROCEDURE dbo.load_target AS
    INSERT INTO dbo.Target (id, col_a, col_b, col_c, col_d) SELECT id, col_a, col_b, col_a, col_b FROM dbo.Feed;
GO
CREATE PROCEDURE dbo.fix_a AS
    UPDATE t SET col_a = f.col_a FROM dbo.Target AS t JOIN dbo.Fix AS f ON f.id = t.id;
GO
CREATE PROCEDURE dbo.fix_b AS
    UPDATE t SET col_b = f.col_b FROM dbo.Target AS t JOIN dbo.Fix AS f ON f.id = t.id;
GO
CREATE PROCEDURE dbo.fix_c AS
    UPDATE t SET col_c = f.col_a FROM dbo.Target AS t JOIN dbo.Fix AS f ON f.id = t.id;
GO
CREATE PROCEDURE dbo.fix_d AS
    UPDATE t SET col_d = f.col_b FROM dbo.Target AS t JOIN dbo.Fix AS f ON f.id = t.id;
GO
CREATE PROCEDURE dbo.run_all AS
BEGIN
    EXEC dbo.fix_b;
    EXEC dbo.fix_a;
END
GO
