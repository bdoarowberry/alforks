CREATE TABLE stage.src (id int, code varchar(10));
GO
CREATE TABLE stage.feed (id int, code varchar(10));
GO
CREATE TABLE dbo.out (id int, code varchar(10));
GO
CREATE VIEW stage.v_src AS SELECT id, code FROM stage.src;
GO
CREATE PROCEDURE dbo.load_out AS
INSERT INTO dbo.out (id, code) SELECT s.id, s.code FROM stage.src s;
GO
-- Writes stage.src through an alias; the statement does not parse (stray token after SET).
CREATE PROCEDURE stage.fix_src AS
UPDATE s SET s.code = f.code,, FROM stage.src s JOIN stage.feed f ON f.id = s.id;
GO
-- Writes stage.src through an updatable view.
CREATE PROCEDURE stage.fix_src_view AS
UPDATE stage.v_src SET code = 'X' WHERE id = 1;
GO
-- Writes stage.src through a CTE.
CREATE PROCEDURE stage.fix_src_cte AS
WITH c AS (SELECT id, code FROM stage.src) UPDATE c SET code = 'Y' WHERE id = 2;
GO
