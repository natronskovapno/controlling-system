-- Controlling system: database schema (structure only, no data)
-- Generated from the live schema; obsolete/backup tables omitted.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS account_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_number TEXT NOT NULL,
    account_label TEXT,
    closing_balance REAL,
    balance_date TEXT NOT NULL,
    statement_number INTEGER,
    source_file TEXT,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    field_adjusted TEXT NOT NULL,
    original_value REAL,
    corrected_value REAL,
    reason TEXT,
    adjustment_date TEXT DEFAULT (date('now')),
    source TEXT,
    recorded_by TEXT
);

CREATE TABLE IF NOT EXISTS bank_transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    account TEXT NOT NULL,
    statement_number INTEGER,
    amount REAL NOT NULL,
    direction TEXT NOT NULL,
    counterparty TEXT,
    purpose TEXT,
    reference_in TEXT,
    reference_out TEXT,
    currency TEXT DEFAULT 'RSD',
    allocated INTEGER DEFAULT 0,
    entity TEXT DEFAULT 'main',
    source TEXT DEFAULT 'XML',
    raw_xml_row TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    fitid TEXT,
    auto_tag TEXT,
    manual_tag TEXT,
    notes TEXT,
    purposecode TEXT,
    is_internal_transfer INTEGER DEFAULT 0,
    trnplace TEXT,
    counterparty_account TEXT,
    counterparty_bank TEXT,
    payment_period TEXT
);

CREATE TABLE IF NOT EXISTS bb_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    konto TEXT NOT NULL,
    naziv TEXT,
    ps_d REAL DEFAULT 0,
    ps_p REAL DEFAULT 0,
    tp_d REAL DEFAULT 0,
    tp_p REAL DEFAULT 0,
    uk_d REAL DEFAULT 0,
    uk_p REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bb_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_from TEXT NOT NULL,
    period_to TEXT NOT NULL,
    company TEXT,
    pib TEXT,
    fmt TEXT,
    source_file TEXT,
    stored_path TEXT,
    file_hash TEXT,
    is_closing INTEGER DEFAULT 0,
    line_count INTEGER,
    checks_json TEXT,
    warnings_json TEXT,
    checks_ok INTEGER,
    notes TEXT,
    imported_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS bill_items_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    venue TEXT,
    product_code TEXT,
    product_name TEXT,
    category_id INTEGER,
    quantity REAL DEFAULT 0,
    price REAL DEFAULT 0,
    tax_percent REAL DEFAULT 0,
    total_price REAL DEFAULT 0,
    discount_amount REAL DEFAULT 0,
    input_price_net REAL DEFAULT 0,
    input_price_gross REAL DEFAULT 0,
    total_cogs_net REAL DEFAULT 0,
    stock_warehouse TEXT,
    ingested_at TEXT,
    line_sequence INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS bills_raw (
    bill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fiscal_receipt_number TEXT,
    fiscal_receipt_counter TEXT,
    time_created TEXT,
    date TEXT,
    hour_slot INTEGER,
    computer_name TEXT,
    venue TEXT,
    table_name TEXT,
    user_full_name TEXT,
    amount REAL DEFAULT 0,
    is_revoked INTEGER DEFAULT 0,
    original_id INTEGER,
    invoice_type TEXT,
    business_partner_tax TEXT,
    business_partner_name TEXT,
    payment_types_json TEXT,
    is_reprezentacija INTEGER DEFAULT 0,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS cogs_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    warehouse_id INTEGER,
    product_id INTEGER,
    product_name TEXT,
    quantity REAL DEFAULT 0,
    sales_gross REAL DEFAULT 0,
    sales_net REAL DEFAULT 0,
    sales_price_gross REAL DEFAULT 0,
    sales_price_net REAL DEFAULT 0,
    cogs_gross REAL DEFAULT 0,
    cogs_net REAL DEFAULT 0,
    unit_cost_gross REAL DEFAULT 0,
    unit_cost_net REAL DEFAULT 0,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS date_table (
    date TEXT PRIMARY KEY,
    day_of_week TEXT NOT NULL,
    day_of_week_sr TEXT NOT NULL,
    week_number INTEGER NOT NULL,
    month INTEGER NOT NULL,
    month_name_sr TEXT NOT NULL,
    quarter INTEGER NOT NULL,
    year INTEGER NOT NULL,
    is_weekend INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS debit_card_transactions (
    card_tx_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    counterparty TEXT,
    amount REAL NOT NULL,
    classification TEXT DEFAULT 'Unclassified',
    event_id INTEGER,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS document_items_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    document_date TEXT,
    product_id INTEGER,
    product_code TEXT,
    product_name TEXT,
    category_name TEXT,
    quantity REAL DEFAULT 0,
    price REAL DEFAULT 0,
    price_total REAL DEFAULT 0,
    tax_percent REAL DEFAULT 0,
    unit_measure TEXT,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS documents_raw (
    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_type_id INTEGER,
    document_type_name TEXT,
    full_number TEXT,
    document_date TEXT,
    due_date TEXT,
    entered_user TEXT,
    business_partner_name TEXT,
    business_partner_tax TEXT,
    stock_warehouse_name TEXT,
    to_stock_warehouse_name TEXT,
    amount_without_tax REAL DEFAULT 0,
    amount_tax REAL DEFAULT 0,
    affect_stock INTEGER DEFAULT 0,
    is_deleted INTEGER DEFAULT 0,
    description TEXT,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS electricity_split (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    garden_kwh REAL,
    brewery_kwh REAL,
    total_kwh REAL,
    garden_amount REAL,
    brewery_amount REAL,
    total_amount REAL
);

CREATE TABLE IF NOT EXISTS employee_bracket_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_name TEXT NOT NULL,
    bracket TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tax_bracket TEXT NOT NULL DEFAULT 'unknown',
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    birth_date TEXT
);

CREATE TABLE IF NOT EXISTS event_type_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events_daily_log (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    month_year TEXT,
    venue TEXT NOT NULL,
    organiser TEXT,
    event_name TEXT,
    event_type TEXT,
    event_start TEXT,
    event_end TEXT,
    number_of_visitors INTEGER,
    sales_gross REAL DEFAULT 0,
    third_party_ticket_revenue REAL DEFAULT 0,
    venue_rent REAL DEFAULT 0,
    ticket_sales_own REAL DEFAULT 0,
    other_revenues REAL DEFAULT 0,
    cogs REAL DEFAULT 0,
    wages REAL DEFAULT 0,
    musicians_fee REAL DEFAULT 0,
    equipment_expenses REAL DEFAULT 0,
    laborers REAL DEFAULT 0,
    advertising REAL DEFAULT 0,
    debit_card_expenses_variable REAL DEFAULT 0,
    other_expenses REAL DEFAULT 0,
    fixed_profit REAL DEFAULT 0,
    status TEXT DEFAULT 'Preliminary',
    additional_details TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    last_modified_at TEXT DEFAULT (datetime('now')),
    modified_by TEXT,
    bank_linked_costs REAL DEFAULT 0,
    source_ref TEXT,
    sales_net_auto REAL,
    business_bills_net_auto REAL,
    third_party_auto REAL,
    cogs_auto REAL,
    wages_auto REAL,
    laborers_auto REAL,
    sales_net_override REAL,
    business_bills_override REAL,
    third_party_override REAL,
    cogs_override REAL,
    wages_override REAL,
    laborers_override REAL,
    venue_rent_override REAL,
    ticket_sales_own_override REAL,
    venue_rent_invoice_id INTEGER,
    musicians_fee_invoice_id INTEGER,
    equipment_invoice_id INTEGER,
    advertising_invoice_id INTEGER,
    ticket_sales_bank_tx_id INTEGER
);

CREATE TABLE IF NOT EXISTS fixed_cost_tags (
    tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'expense',
    description TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    submodule TEXT NOT NULL DEFAULT 'operativni',
    venue TEXT NOT NULL DEFAULT 'shared'
);

CREATE TABLE IF NOT EXISTS fixed_costs_monthly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    electricity_garden REAL DEFAULT 0,
    electricity_corner REAL DEFAULT 0,
    rent REAL DEFAULT 0,
    water REAL DEFAULT 0,
    salaries REAL DEFAULT 0,
    other REAL DEFAULT 0,
    days_in_month INTEGER
);

CREATE TABLE IF NOT EXISTS fixed_income_monthly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    parking_income REAL DEFAULT 0,
    sublease_income REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fixed_other_monthly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    bookkeeper REAL DEFAULT 0,
    music_royalties_a REAL DEFAULT 0,
    music_royalties_b REAL DEFAULT 0,
    cleaning REAL DEFAULT 0,
    maintenance REAL DEFAULT 0,
    social_media REAL DEFAULT 0,
    pos_provider REAL DEFAULT 0,
    led_signage REAL DEFAULT 0,
    produce REAL DEFAULT 0,
    parking_ramp REAL DEFAULT 0,
    debit_card_fixed REAL DEFAULT 0,
    other REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS foreign_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    account TEXT,
    direction TEXT NOT NULL,
    amount_foreign REAL NOT NULL,
    currency TEXT NOT NULL,
    exchange_rate REAL,
    amount_rsd REAL,
    counterparty TEXT,
    purpose TEXT,
    reference_number TEXT,
    statement_number TEXT,
    notes TEXT,
    entity TEXT NOT NULL DEFAULT 'main',
    created_at TEXT NOT NULL,
    created_by TEXT
);

CREATE TABLE IF NOT EXISTS fs_exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    konto TEXT NOT NULL,
    amount REAL NOT NULL,
    reason TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS fs_mapping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    konto_prefix TEXT NOT NULL,
    report_line TEXT NOT NULL,
    note TEXT,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS fs_params (
    key TEXT PRIMARY KEY,
    value TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS fx_bank_statements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT,
    iban TEXT,
    currency TEXT,
    subaccount_code TEXT,
    subaccount_name TEXT,
    statement_number TEXT,
    statement_date TEXT,
    previous_balance REAL,
    new_balance REAL,
    source_file TEXT,
    uploaded_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fx_bank_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    statement_id INTEGER NOT NULL,
    line_no INTEGER,
    description TEXT,
    debit REAL DEFAULT 0,
    credit REAL DEFAULT 0,
    rsd_equivalent REAL DEFAULT 0,
    booking_date TEXT,
    kurs REAL,
    nalog_br TEXT,
    predmet_br TEXT
);

CREATE TABLE IF NOT EXISTS ingestion_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_detected TEXT NOT NULL,
    source TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    resolved_at TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ingestion_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    source TEXT NOT NULL,
    date_from TEXT,
    date_to TEXT,
    records_found INTEGER DEFAULT 0,
    records_new INTEGER DEFAULT 0,
    records_skipped INTEGER DEFAULT 0,
    records_error INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    error_detail TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS inventory_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    product_code TEXT NOT NULL,
    product_name TEXT,
    category_id INTEGER,
    venue TEXT NOT NULL DEFAULT 'unknown',
    quantity_sold REAL DEFAULT 0,
    revenue_gross REAL DEFAULT 0,
    discount_amount REAL DEFAULT 0,
    cogs_net REAL DEFAULT 0,
    tax_percent REAL DEFAULT 0,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS inventory_shift_cursor (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    last_completed_date TEXT,
    last_completed_shift INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS inventory_stock_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    product_id INTEGER,
    product_code TEXT,
    product_name TEXT,
    category_name TEXT,
    group_name TEXT,
    unit_measure TEXT,
    closing_quantity REAL DEFAULT 0,
    shift_id INTEGER,
    warehouse_name TEXT,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS invoice_fixed_cost_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    amount REAL NOT NULL,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    vat_rate REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invoice_line_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id_sef INTEGER NOT NULL,
    invoice_type TEXT NOT NULL DEFAULT 'purchase',
    row_id INTEGER,
    order_no INTEGER,
    code TEXT,
    description TEXT,
    unit TEXT,
    unit_price REAL,
    quantity REAL,
    discount_percentage REAL,
    discount_amount REAL,
    net_amount REAL,
    vat_rate REAL,
    vat_amount REAL,
    gross_amount REAL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS invoice_payment_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    invoice_id INTEGER NOT NULL,
    payment_date TEXT NOT NULL,
    amount REAL NOT NULL,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invoices_in (
    invoice_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_issued TEXT,
    date_received TEXT,
    due_date TEXT,
    supplier_name TEXT NOT NULL,
    supplier_pib TEXT,
    invoice_number_sef TEXT,
    invoice_number_ext TEXT,
    net_amount REAL NOT NULL DEFAULT 0,
    pdv_amount REAL DEFAULT 0,
    pdv_rate REAL DEFAULT 0,
    pdv_category TEXT,
    payment_status TEXT DEFAULT 'Unpaid',
    entity TEXT DEFAULT 'main',
    notes TEXT,
    source TEXT DEFAULT 'SEF',
    created_at TEXT DEFAULT (datetime('now')),
    invoice_status TEXT,
    supplier_category TEXT,
    document_type TEXT,
    currency TEXT DEFAULT 'RSD',
    raw_json TEXT,
    ingested_at TEXT,
    invoice_id_sef INTEGER,
    amount_paid REAL DEFAULT 0,
    payment_date TEXT
);

CREATE TABLE IF NOT EXISTS invoices_out (
    invoice_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_issued TEXT,
    due_date TEXT,
    client_name TEXT NOT NULL,
    client_pib TEXT,
    invoice_number_sef TEXT,
    invoice_number_ext TEXT,
    net_amount REAL NOT NULL DEFAULT 0,
    pdv_amount REAL DEFAULT 0,
    pdv_rate REAL DEFAULT 0,
    pdv_category TEXT,
    payment_status TEXT DEFAULT 'Unpaid',
    entity TEXT DEFAULT 'main',
    notes TEXT,
    source TEXT DEFAULT 'SEF',
    created_at TEXT DEFAULT (datetime('now')),
    invoice_status TEXT,
    document_type TEXT,
    currency TEXT DEFAULT 'RSD',
    raw_json TEXT,
    ingested_at TEXT,
    invoice_id_sef INTEGER,
    amount_paid REAL DEFAULT 0,
    payment_date TEXT
);

CREATE TABLE IF NOT EXISTS brewery_invoices_in (
    invoice_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_issued TEXT,
    date_received TEXT,
    due_date TEXT,
    supplier_name TEXT NOT NULL,
    supplier_pib TEXT,
    invoice_number_sef TEXT,
    net_amount REAL DEFAULT 0,
    pdv_amount REAL DEFAULT 0,
    pdv_rate REAL DEFAULT 0,
    payment_status TEXT DEFAULT 'Unpaid',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS brewery_invoices_out (
    invoice_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_issued TEXT,
    due_date TEXT,
    client_name TEXT NOT NULL,
    client_pib TEXT,
    invoice_number_sef TEXT,
    net_amount REAL DEFAULT 0,
    pdv_amount REAL DEFAULT 0,
    pdv_rate REAL DEFAULT 0,
    payment_status TEXT DEFAULT 'Unpaid',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS brewery_transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    direction TEXT NOT NULL,
    counterparty TEXT,
    amount REAL NOT NULL,
    purpose TEXT,
    reference TEXT,
    account TEXT,
    statement_number INTEGER,
    bank_tx_id INTEGER,
    purposecode TEXT,
    currency TEXT DEFAULT 'RSD',
    entity TEXT DEFAULT 'brewery',
    source TEXT DEFAULT 'PDF',
    fitid TEXT,
    created_at TEXT,
    is_internal_transfer INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pos_ingestion_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    records_total INTEGER DEFAULT 0,
    records_new INTEGER DEFAULT 0,
    records_updated INTEGER DEFAULT 0,
    items_written INTEGER DEFAULT 0,
    status TEXT,
    run_at TEXT
);

CREATE TABLE IF NOT EXISTS output_invoices_raw (
    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_number TEXT,
    document_date TEXT NOT NULL,
    due_date TEXT,
    business_partner_name TEXT,
    business_partner_tax TEXT,
    fiscal_receipt_number TEXT,
    amount_net REAL DEFAULT 0,
    amount_vat REAL DEFAULT 0,
    amount_gross REAL DEFAULT 0,
    payed_amount REAL DEFAULT 0,
    description TEXT,
    is_deleted INTEGER DEFAULT 0,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS passthrough_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS payment_hourly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    venue TEXT NOT NULL,
    hour_slot INTEGER NOT NULL,
    payment_type_name TEXT NOT NULL DEFAULT 'all',
    amount REAL NOT NULL DEFAULT 0,
    bill_count INTEGER DEFAULT 0,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS payment_proposal (
    invoice_id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed_amount REAL NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS payments_made (
    payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    receiver TEXT,
    purpose TEXT,
    reference TEXT,
    amount_paid REAL NOT NULL,
    invoice_id INTEGER,
    transaction_id INTEGER
);

CREATE TABLE IF NOT EXISTS payments_received (
    payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    sender TEXT,
    purpose TEXT,
    reference TEXT,
    amount_received REAL NOT NULL,
    invoice_id INTEGER,
    transaction_id INTEGER
);

CREATE TABLE IF NOT EXISTS pdv_working (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    stream TEXT NOT NULL,
    pdv_amount REAL DEFAULT 0,
    direction TEXT,
    notes TEXT,
    calculated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prenosi_manual_payments (
    fiscal_receipt_number TEXT PRIMARY KEY,
    paid_amount REAL DEFAULT 0,
    paid_date TEXT,
    notes TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS product_categories (
    category_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    updated_at TEXT,
    parent_id INTEGER
);

CREATE TABLE IF NOT EXISTS product_components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER NOT NULL,
    parent_code TEXT,
    child_id INTEGER NOT NULL,
    child_code TEXT,
    quantity REAL DEFAULT 0,
    shrinkage_pct REAL DEFAULT 0,
    updated_at TEXT,
    component_id INTEGER,
    unit TEXT
);

CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT,
    category_id INTEGER,
    category_name TEXT,
    unit_measure TEXT,
    tax_percent REAL,
    selling_price REAL,
    is_active INTEGER DEFAULT 1,
    updated_at TEXT,
    input_price REAL
);

CREATE TABLE IF NOT EXISTS reprezentacija (
    bill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    time_created TEXT,
    user_full_name TEXT,
    table_name TEXT,
    computer_name TEXT,
    venue TEXT,
    amount_gross REAL DEFAULT 0,
    payment_types_json TEXT,
    is_revoked INTEGER DEFAULT 0,
    ingested_at TEXT,
    amount_net REAL DEFAULT 0,
    pdv REAL DEFAULT 0,
    vip_guest_id INTEGER,
    vip_guest_name TEXT,
    amount_paid REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reprezentacija_documents (
    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_number TEXT,
    date TEXT NOT NULL,
    category TEXT,
    employee_name TEXT,
    business_partner_name TEXT,
    amount_net REAL DEFAULT 0,
    amount_vat REAL DEFAULT 0,
    amount_gross REAL DEFAULT 0,
    description TEXT,
    is_deleted INTEGER DEFAULT 0,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS salaries_monthly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_name TEXT NOT NULL,
    role TEXT,
    monthly_amount REAL NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    monthly_gross REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sales_hourly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    venue TEXT NOT NULL,
    hour_slot INTEGER NOT NULL,
    product_name TEXT NOT NULL,
    category_id INTEGER,
    quantity REAL NOT NULL DEFAULT 0,
    revenue_net REAL NOT NULL DEFAULT 0,
    revenue_gross REAL NOT NULL DEFAULT 0,
    ingested_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stock_warehouses (
    warehouse_id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT,
    name TEXT,
    is_active INTEGER DEFAULT 1,
    updated_at TEXT,
    location_name TEXT
);

CREATE TABLE IF NOT EXISTS wages_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    employee_name TEXT NOT NULL,
    worker_type TEXT,
    venue TEXT,
    net_amount REAL NOT NULL,
    tax_bracket TEXT,
    gross_cost REAL DEFAULT 0,
    source_file TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    fitid TEXT
);

CREATE TABLE IF NOT EXISTS web_analytics_breakdown (
    site_tag TEXT,
    date TEXT,
    dim_type TEXT,
    dim_value TEXT,
    bot INTEGER,
    pageviews INTEGER NOT NULL DEFAULT 0,
    visits INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (site_tag, date, dim_type, dim_value, bot)
);

CREATE TABLE IF NOT EXISTS web_analytics_cursor (
    site_tag TEXT PRIMARY KEY,
    host TEXT,
    last_date TEXT,
    last_run_at TEXT,
    last_status TEXT,
    last_error TEXT,
    days_ingested INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS web_analytics_daily (
    site_tag TEXT,
    date TEXT,
    bot INTEGER,
    pageviews INTEGER NOT NULL DEFAULT 0,
    visits INTEGER NOT NULL DEFAULT 0,
    sample_interval REAL,
    ingested_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (site_tag, date, bot)
);

CREATE TABLE IF NOT EXISTS web_analytics_hourly (
    site_tag TEXT,
    date TEXT,
    hour INTEGER,
    bot INTEGER,
    pageviews INTEGER NOT NULL DEFAULT 0,
    visits INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (site_tag, date, hour, bot)
);

