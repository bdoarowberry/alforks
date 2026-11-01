USE [msdb]
GO
DECLARE @jobId BINARY(16)
EXEC msdb.dbo.sp_add_job @job_name=N'Afternoon fix', @enabled=1, @job_id = @jobId OUTPUT
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'Fix c', @step_id=1, @on_success_action=1,
		@subsystem=N'TSQL', @command=N'EXEC dbo.fix_c', @database_name=N'demo'
GO
DECLARE @jobId BINARY(16)
EXEC msdb.dbo.sp_add_job @job_name=N'Old fix', @enabled=0, @job_id = @jobId OUTPUT
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'Fix d', @step_id=1, @on_success_action=1,
		@subsystem=N'TSQL', @command=N'EXEC dbo.fix_d', @database_name=N'demo'
GO
