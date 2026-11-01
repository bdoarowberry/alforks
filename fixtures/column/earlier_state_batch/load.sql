CREATE TABLE dbo.Raw (amount int);
CREATE TABLE dbo.Doubled (amount int);
CREATE TABLE dbo.Copy (first_amount int);

INSERT INTO dbo.Doubled (amount)
SELECT amount FROM dbo.Raw;
UPDATE dbo.Doubled SET amount = amount * 2;
INSERT INTO dbo.Copy (first_amount)
SELECT amount FROM dbo.Doubled;
