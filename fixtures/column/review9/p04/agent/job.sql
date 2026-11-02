USE [msdb]
GO
DECLARE @jobId BINARY(16)
EXEC msdb.dbo.sp_add_job @job_name=N'Nightly', @enabled=1, @job_id = @jobId OUTPUT
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'two', @step_id=1, @subsystem=N'TSQL', @command=N'EXEC dbo.w_two', @database_name=N'demo', @on_success_action=3, @on_fail_action=2
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'one', @step_id=2, @subsystem=N'TSQL', @command=N'EXEC dbo.w_one', @database_name=N'demo', @on_success_action=1, @on_fail_action=2
EXEC msdb.dbo.sp_add_jobschedule @job_id=@jobId, @name=N'daily', @enabled=1, @freq_type=4, @freq_interval=1, @active_start_time=10000
EXEC msdb.dbo.sp_update_job @job_id = @jobId, @start_step_id = 1
GO
