-- Table: files_to_import
-- Purpose: Tracks radiosonde data files scheduled for import into the database
-- Description: Maintains a queue of files to be processed, preventing duplicate imports
--              through unique constraints and tracking import timestamps
-- Context: Used in automated data ingestion pipeline for radiosonde observations

DROP TABLE IF EXISTS files_to_import CASCADE;

CREATE TABLE files_to_import
(
    sonde          CHAR(10)     NOT NULL,
    filename       VARCHAR(255) NOT NULL,
    date_of_import TIMESTAMPTZ  NULL,
    note           text         NULL,
    CONSTRAINT files_to_import_unique UNIQUE (sonde, filename)
);