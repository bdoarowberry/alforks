CREATE TABLE dbo.cust_src (cust_id int, cust_name varchar(50));
GO
CREATE TABLE dbo.cust_dim (cust_id int, cust_name varchar(50));
GO
CREATE TABLE dbo.cust_arch (cust_id int, cust_name varchar(50));
GO
CREATE TABLE dbo.cust_hist (act nvarchar(10), cust_name varchar(50));
GO
CREATE PROCEDURE dbo.archive_cust AS
BEGIN
    DELETE FROM dbo.cust_dim
    OUTPUT deleted.cust_id, deleted.cust_name INTO dbo.cust_arch (cust_id, cust_name)
    WHERE cust_id < 0;
END
GO
CREATE PROCEDURE dbo.merge_hist AS
BEGIN
    INSERT INTO dbo.cust_hist (act, cust_name)
    SELECT m.act, m.nm
    FROM (
        MERGE dbo.cust_dim AS t
        USING dbo.cust_src AS s ON t.cust_id = s.cust_id
        WHEN MATCHED THEN UPDATE SET t.cust_name = s.cust_name
        WHEN NOT MATCHED THEN INSERT (cust_id, cust_name) VALUES (s.cust_id, s.cust_name)
        OUTPUT $action AS act, deleted.cust_name AS nm
    ) AS m
    WHERE m.act = 'UPDATE';
END
GO
