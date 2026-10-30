CREATE TABLE dbo.item (id int, amount int);
CREATE TABLE dbo.amount_history (id int, old_amount int);
CREATE TABLE dbo.feed (id int, new_amount int);
GO
CREATE PROCEDURE dbo.apply_feed AS
BEGIN
    UPDATE dbo.item
    SET amount = f.new_amount
    OUTPUT deleted.id, deleted.amount INTO amount_history
    FROM dbo.item i JOIN dbo.feed f ON f.id = i.id;
END
GO
