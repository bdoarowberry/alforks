CREATE TABLE demo.source (id int NOT NULL, event_date date NULL, other int NULL);
GO
CREATE TABLE demo.labels (id int NOT NULL, label varchar(20) NULL);
GO
CREATE TABLE demo.output (id int NOT NULL, date_key int NULL, wrapped int NULL, label varchar(20) NULL);
GO
CREATE PROCEDURE demo.load_output
AS
INSERT INTO demo.output (id, date_key, wrapped, label)
SELECT s.id, dbo.fn_date_key(s.event_date, s.other), dbo.fn_wrap(s.event_date), dbo.fn_lookup(s.id)
FROM demo.source AS s;
GO
