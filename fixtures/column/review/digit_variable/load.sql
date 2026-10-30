CREATE TABLE demo.source (id int, input_a int, input_b int, event_date date);
GO
CREATE PROCEDURE demo.load_digit @2_periods_ago date AS
BEGIN
    DECLARE @1_cutoff date = DATEADD(day, -1, @2_periods_ago);
    INSERT INTO demo.output (id, result_value)
    SELECT s.id, CASE WHEN s.event_date = @2_periods_ago THEN s.input_a ELSE s.input_b END
    FROM demo.source s
    WHERE s.event_date >= @1_cutoff;
END
GO
