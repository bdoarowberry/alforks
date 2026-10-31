CREATE TABLE stage.src (id int, amount int, note varchar(10));
GO
CREATE TABLE dbo.out (id int, total int, label varchar(40));
GO
CREATE PROCEDURE dbo.add_amounts AS
UPDATE o SET o.total += s.amount -- running total
FROM dbo.out o JOIN stage.src s ON s.id = o.id;
GO
CREATE PROCEDURE dbo.add_label AS
UPDATE o SET o.label += s.note -- suffix
    , o.id = o.id
FROM dbo.out o JOIN stage.src s ON s.id = o.id;
GO
