-- database/init.sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_database WHERE datname = 'coeus_db') THEN
        CREATE DATABASE "coeus_db";
    END IF;
END $$;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------
-- 1. Entities Table (e.g. Companies, Organizations)
-- ---------------------------------------------------------
CREATE TABLE entities (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL UNIQUE,
    identifier VARCHAR(50), -- e.g. Ticker, Registration Number
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------
-- 2. Targets Table (e.g. Projects, Assets, Locations)
-- ---------------------------------------------------------
CREATE TABLE targets (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_name VARCHAR(255) NOT NULL,
    location VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(entity_id, target_name)
);

-- ---------------------------------------------------------
-- 3. Extracted Records Table (The Core Data)
-- ---------------------------------------------------------
CREATE TABLE extracted_records (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    target_id UUID NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    document_date DATE NOT NULL,
    record_type VARCHAR(100) NOT NULL, -- e.g. 'Resource Estimate', 'Financial Performance'

    -- Generic industry-specific data stored as JSONB
    data JSONB NOT NULL,

    -- Data Quality & Pipeline Metadata
    requires_human_review BOOLEAN DEFAULT FALSE,
    review_reason TEXT,
    source_url TEXT,
    extracted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(target_id, document_date, record_type)
);

-- ---------------------------------------------------------
-- 4. Indexes for Query Optimization
-- ---------------------------------------------------------
CREATE INDEX idx_targets_entity ON targets(entity_id);
CREATE INDEX idx_records_target ON extracted_records(target_id);
CREATE INDEX idx_records_review ON extracted_records(requires_human_review) WHERE requires_human_review = TRUE;
CREATE INDEX idx_records_data ON extracted_records USING gin (data);
