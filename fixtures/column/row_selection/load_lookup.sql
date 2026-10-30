CREATE PROCEDURE demo.load_lookup
AS
BEGIN
    INSERT INTO demo.lookup (id, region_code)
    SELECT r.id, r.region_code
    FROM demo.lookup_raw AS r;
END;
GO
