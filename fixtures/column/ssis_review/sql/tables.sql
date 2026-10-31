-- Invented tables for the SSIS adapter review fixtures (review-fixtures/*).
USE rev_db;
GO
CREATE TABLE rv.src (a int NULL, b int NULL, c int NULL);
CREATE TABLE rv.src2 (a int NULL, b int NULL);
CREATE TABLE rv.ref (k int NOT NULL, label varchar(20) NULL);
CREATE TABLE rv.tgt (x int NULL, y int NULL, z int NULL);
GO
USE other_db;
GO
CREATE TABLE rv.tgt (x int NULL, y int NULL, z int NULL);
GO
