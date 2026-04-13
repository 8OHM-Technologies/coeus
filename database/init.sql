-- database/init.sql

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------
-- 1. Companies Table
-- ---------------------------------------------------------
CREATE TABLE companies (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL UNIQUE,
    ticker VARCHAR(50),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------
-- 2. Assets Table (The Mines/Projects)
-- ---------------------------------------------------------
CREATE TABLE assets (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    asset_name VARCHAR(255) NOT NULL,
    location VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(company_id, asset_name)
);

-- ---------------------------------------------------------
-- 3. Resource Estimates Table (The Core Data)
-- ---------------------------------------------------------
CREATE TABLE resource_estimates (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    asset_id UUID NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    effective_date DATE NOT NULL,
    classification VARCHAR(50) NOT NULL, -- e.g., 'Measured', 'Indicated', 'Inferred'
    commodity VARCHAR(50) NOT NULL,      -- e.g., 'Gold', 'Zinc', 'Copper'
    tonnage_mt NUMERIC(10, 2),
    grade NUMERIC(10, 2),
    grade_unit VARCHAR(20),              -- e.g., 'g/t', '%'

    -- Data Quality & Pipeline Metadata
    requires_human_review BOOLEAN DEFAULT FALSE,
    review_reason TEXT,
    source_url TEXT,
    extracted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(asset_id, effective_date, classification, commodity)
);

-- ---------------------------------------------------------
-- 4. Indexes for Query Optimization
-- ---------------------------------------------------------
CREATE INDEX idx_assets_company ON assets(company_id);
CREATE INDEX idx_estimates_asset ON resource_estimates(asset_id);
CREATE INDEX idx_estimates_review ON resource_estimates(requires_human_review) WHERE requires_human_review = TRUE;
