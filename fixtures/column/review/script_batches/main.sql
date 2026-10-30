SELECT s.id, s.input_a AS v INTO #stage FROM demo.source s;
GO
INSERT INTO demo.output (id, result_value) SELECT st.id, st.v FROM #stage st;
GO
