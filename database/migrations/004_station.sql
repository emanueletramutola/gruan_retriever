-- Table: station
-- Purpose: Master reference table for all weather stations with essential metadata
-- Description: Stores core station information including geographical coordinates,
--              identification codes, and network affiliations
-- Context: Central reference table for linking observational data to station locations

DROP TABLE IF EXISTS station CASCADE;

CREATE TABLE station
(
    id                          serial,
--     continent                   character varying(2),
--     countrycode                 character varying(2),
    elevation                   real,
    idstation                   character varying(11),
    latitude                    real,
    longitude                   real,
    name                        character varying(255),
    network                     character varying(50),
    wmoid                       integer,
    PRIMARY KEY (id),
    CONSTRAINT station_idstation_unique UNIQUE (idstation)
);