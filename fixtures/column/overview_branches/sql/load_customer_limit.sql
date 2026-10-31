USE rpt_db;
GO
CREATE PROCEDURE report.load_customer_limit
AS
BEGIN
    -- Main branch: a closed customer gets 0, otherwise the customer limit, else the account default.
    INSERT INTO report.customer_limit (customer_id, credit_limit, source_code)
    SELECT c.customer_id,
           CASE WHEN c.is_closed = 1 THEN 0
                ELSE COALESCE(c.credit_limit, a.default_limit)
           END,
           'CORE'
    FROM core_db.crm.customer AS c
    LEFT JOIN core_db.crm.account AS a ON a.customer_id = c.customer_id
    WHERE c.is_active = 1;

    -- Partner branch: a staging table nobody in these sources writes.
    INSERT INTO report.customer_limit (customer_id, credit_limit, source_code)
    SELECT p.customer_id, p.credit_limit, 'PARTNER'
    FROM partner_db.stage.partner_extract AS p
    WHERE p.batch_ok = 1;

    -- File branch: staged by the package load_file_limit.dtsx.
    INSERT INTO report.customer_limit (customer_id, credit_limit, source_code)
    SELECT f.customer_id, CAST(f.limit_text AS decimal(18, 2)), 'FILE'
    FROM stage.file_limit AS f;

    -- Override: approved manual adjustments replace the loaded value.
    UPDATE t
    SET credit_limit = adj.new_limit
    FROM report.customer_limit AS t
    JOIN core_db.crm.limit_adjustment AS adj ON adj.customer_id = t.customer_id
    WHERE adj.approved = 1;
END;
GO
