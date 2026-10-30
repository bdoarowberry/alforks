CREATE TABLE dbo.item (id int, amount int);
CREATE TABLE dbo.change_log (id int, old_amount int);
CREATE TABLE dbo.feed (id int, new_amount int);
GO
CREATE PROCEDURE dbo.apply_feed AS
BEGIN
    CREATE TABLE #changes (id int, old_amount int);
    UPDATE dbo.item
    SET amount = f.new_amount
    OUTPUT deleted.id, deleted.amount INTO #changes
    FROM dbo.item i JOIN dbo.feed f ON f.id = i.id;
    INSERT INTO dbo.change_log (id, old_amount) SELECT c.id, c.old_amount FROM #changes c;
END
GO
