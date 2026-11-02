DECLARE @jobId BINARY(16), @n int = 1
EXEC msdb.dbo.sp_add_job @job_name=N'J', @enabled=1, @job_id = @jobId OUTPUT
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'one', @step_id=@n, @on_success_action=3, @subsystem=N'TSQL', @command=N'EXEC dbo.w_one', @database_name=N'demo'
GO
