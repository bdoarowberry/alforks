EXEC dbo.load @p = 42;
GO
INSERT INTO db1.dbo.out1 (id, val) SELECT x.id, x.a FROM dbo.src x;
GO
