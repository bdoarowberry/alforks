CREATE TABLE dbo.SourceTable (id int, col_a int, flag int);
CREATE TABLE dbo.Twice (id int, col_a int);
CREATE TABLE dbo.WithIdentity (id int IDENTITY(1, 1), col_a int);
CREATE TABLE dbo.Added (id int);
GO
ALTER TABLE dbo.Added WITH CHECK ADD col_new int;
GO
CREATE TABLE dbo.Late (id int);
CREATE TABLE dbo.LeftTable (id int, col_a int);
CREATE TABLE dbo.RightTable (id int, col_a int);
CREATE TABLE dbo.Parent (id int);
CREATE TABLE dbo.Out_cte (col_a int);
CREATE TABLE dbo.Out_star (col_a int);
CREATE TABLE dbo.Out_identity (col_a int);
CREATE TABLE dbo.Out_added (col_new int);
CREATE TABLE dbo.Out_late (col_new int);
CREATE TABLE dbo.Out_join (col_a int);
CREATE TABLE dbo.Out_exec (amount decimal(12, 2));
CREATE TABLE dbo.Out_two (amount decimal(12, 2));
CREATE TABLE dbo.Out_tvf (col_a int);
CREATE TABLE dbo.Out_backup (col_a int);
CREATE TABLE dbo.Out_identity2 (col_a int);
