CREATE TABLE dbo.Src (id int, v int);
CREATE TABLE dbo.Tgt (id int, v int);
GO
CREATE PROCEDURE dbo.w_one AS
    UPDATE t SET v = s.v + 1 FROM dbo.Tgt AS t JOIN dbo.Src AS s ON s.id = t.id;
GO
CREATE PROCEDURE dbo.w_two AS
    UPDATE t SET v = s.v + 2 FROM dbo.Tgt AS t JOIN dbo.Src AS s ON s.id = t.id;
GO
CREATE PROCEDURE dbo.prep AS
    SELECT 1;
GO
