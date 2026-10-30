CREATE TABLE demo.source (id int, input_a int);
GO
CREATE TABLE stage.dbo.landing (id int, amount int);
GO
CREATE SYNONYM demo.landing_syn FOR stage.dbo.landing;
GO
CREATE PROCEDURE demo.load_land AS
INSERT INTO demo.landing_syn (id, amount) SELECT s.id, s.input_a FROM demo.source s;
GO
