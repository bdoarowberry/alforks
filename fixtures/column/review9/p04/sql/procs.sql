CREATE TABLE dbo.Tgt (k int NOT NULL, v int NULL);
CREATE TABLE dbo.A (k int NOT NULL, x int NULL);
CREATE TABLE dbo.B (k int NOT NULL, y int NULL);
GO
CREATE PROCEDURE dbo.w_one AS
BEGIN
    UPDATE t SET v = a.x FROM dbo.Tgt t JOIN dbo.A a ON a.k = t.k;
END
GO
CREATE PROCEDURE dbo.w_two AS
BEGIN
    UPDATE t SET v = b.y FROM dbo.Tgt t JOIN dbo.B b ON b.k = t.k;
END
GO
CREATE PROCEDURE dbo.run_all AS
BEGIN
    EXEC dbo.w_one;
    EXEC dbo.w_two;
END
