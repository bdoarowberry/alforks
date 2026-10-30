CREATE PROCEDURE demo.seed_alpha
AS
BEGIN
    INSERT INTO demo.alpha (id, val)
    SELECT s.id, s.seed_val
    FROM demo.seed AS s;
END;
GO

CREATE PROCEDURE demo.alpha_to_beta
AS
BEGIN
    INSERT INTO demo.beta (id, val)
    SELECT a.id, a.val + 1
    FROM demo.alpha AS a;
END;
GO

CREATE PROCEDURE demo.beta_to_alpha
AS
BEGIN
    INSERT INTO demo.alpha (id, val)
    SELECT b.id, b.val
    FROM demo.beta AS b;
END;
GO
