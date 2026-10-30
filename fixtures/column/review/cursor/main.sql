CREATE TABLE demo.source (id int, input_a int);
GO
CREATE TABLE demo.output (id int, result_value int);
GO
CREATE PROCEDURE demo.load_cur AS
BEGIN
    DECLARE @id int = 0, @val int = 0;
    DECLARE c CURSOR LOCAL FAST_FORWARD FOR SELECT s.id, s.input_a FROM demo.source s;
    OPEN c;
    FETCH NEXT FROM c INTO @id, @val;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        INSERT INTO demo.output (id, result_value) VALUES (@id, @val);
        FETCH NEXT FROM c INTO @id, @val;
    END
    CLOSE c; DEALLOCATE c;
END
GO
