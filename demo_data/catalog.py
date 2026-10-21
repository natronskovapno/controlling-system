"""Fictional master data for the demo dataset.

Every name, tax ID and figure in this file is invented. Any resemblance to a
real company, person or supplier is coincidental.
"""

# --- Legal entities -------------------------------------------------------
# The demo models a small group: a cultural/hospitality operator running two
# venues, plus a separate brewery company under the same ownership.

ENTITIES = {
    "main": {
        "name": "Vrtna Scena d.o.o.",
        "name_en": "Garden Stage Ltd.",
        "pib": "100000001",
        "vat_registered": True,
    },
    "brewery": {
        "name": "Mala Pivnica d.o.o.",
        "name_en": "Small Brewery Ltd.",
        "pib": "100000002",
        "vat_registered": True,
    },
}

# --- Venues ---------------------------------------------------------------
# "Garden" is the large outdoor summer venue; "Corner" is the small
# year-round indoor bar. Seasonality differs sharply between them.

VENUES = {
    "Garden": {
        "label_sr": "Vrt",
        "label_en": "Garden",
        "entity": "main",
        "indoor": False,
        "capacity": 450,
    },
    "Corner": {
        "label_sr": "Kutak",
        "label_en": "Corner",
        "entity": "main",
        "indoor": True,
        "capacity": 90,
    },
}

WAREHOUSES = [
    (1, "GRD-BAR", "Garden bar", "Garden"),
    (2, "GRD-STK", "Garden stock", "Garden"),
    (3, "COR-BAR", "Corner bar", "Corner"),
    (4, "BRW-STK", "Brewery stock", "Brewery"),
]

# --- Product catalogue ----------------------------------------------------
# (category, name, unit, vat_rate, sell_price_gross, cost_net, popularity)
# Prices in RSD. VAT: 20% standard, 10% reduced (soft drinks, water, food).

CATEGORIES = [
    (1, "Draught beer", None),
    (2, "Bottled beer", None),
    (3, "Spirits", None),
    (4, "Wine", None),
    (5, "Cocktails", None),
    (6, "Soft drinks", None),
    (7, "Coffee & tea", None),
    (8, "Food", None),
    (9, "Tobacco", None),
    (10, "Merchandise", None),
]

PRODUCTS = [
    # cat, name, unit, vat, gross price, net cost, popularity weight
    (1, "Lager 0.33", "pcs", 20, 280, 78, 100),
    (1, "Lager 0.5", "pcs", 20, 380, 112, 140),
    (1, "IPA 0.33", "pcs", 20, 360, 118, 55),
    (1, "Dark ale 0.33", "pcs", 20, 360, 122, 28),
    (1, "Wheat beer 0.5", "pcs", 20, 420, 140, 34),
    (2, "Lager bottle 0.33", "pcs", 20, 300, 96, 44),
    (2, "Cider bottle 0.33", "pcs", 20, 390, 140, 20),
    (2, "Non-alcoholic beer", "pcs", 20, 300, 98, 12),
    (3, "Rakija plum 0.03", "pcs", 20, 260, 64, 72),
    (3, "Rakija quince 0.03", "pcs", 20, 300, 80, 36),
    (3, "Vodka 0.03", "pcs", 20, 320, 72, 40),
    (3, "Gin 0.03", "pcs", 20, 340, 84, 30),
    (3, "Whisky 0.03", "pcs", 20, 420, 128, 26),
    (3, "Tequila 0.03", "pcs", 20, 400, 120, 14),
    (4, "House white 0.1", "pcs", 20, 280, 70, 38),
    (4, "House red 0.1", "pcs", 20, 280, 70, 32),
    (4, "Sparkling 0.1", "pcs", 20, 360, 104, 10),
    (4, "Wine bottle 0.75", "pcs", 20, 2200, 620, 7),
    (5, "Gin & tonic", "pcs", 20, 520, 132, 48),
    (5, "Aperol spritz", "pcs", 20, 560, 148, 42),
    (5, "Mojito", "pcs", 20, 560, 140, 30),
    (5, "Negroni", "pcs", 20, 580, 152, 16),
    (6, "Cola 0.25", "pcs", 10, 220, 58, 60),
    (6, "Tonic 0.25", "pcs", 10, 220, 60, 34),
    (6, "Still water 0.33", "pcs", 10, 160, 32, 70),
    (6, "Sparkling water 0.33", "pcs", 10, 160, 34, 40),
    (6, "Orange juice 0.2", "pcs", 10, 260, 74, 24),
    (6, "Lemonade 0.4", "pcs", 10, 320, 68, 22),
    (7, "Espresso", "pcs", 10, 180, 36, 56),
    (7, "Macchiato", "pcs", 10, 220, 44, 30),
    (7, "Tea", "pcs", 10, 180, 22, 14),
    (8, "Burger", "pcs", 10, 790, 290, 46),
    (8, "Cheese plate", "pcs", 10, 980, 360, 14),
    (8, "Fries", "pcs", 10, 390, 110, 38),
    (8, "Flatbread", "pcs", 10, 690, 230, 20),
    (8, "Nachos", "pcs", 10, 590, 180, 16),
    (9, "Cigarettes", "pcs", 20, 480, 440, 26),
    (10, "T-shirt", "pcs", 20, 2400, 780, 3),
    (10, "Tote bag", "pcs", 20, 1200, 340, 2),
]

# Composite products: cocktails are built from component stock items.
# (parent name, child name, quantity, shrinkage %)
RECIPES = [
    ("Gin & tonic", "Gin 0.03", 1.5, 2.0),
    ("Gin & tonic", "Tonic 0.25", 1.0, 0.0),
    ("Aperol spritz", "Sparkling 0.1", 1.0, 1.0),
    ("Mojito", "Vodka 0.03", 1.5, 2.0),
    ("Negroni", "Gin 0.03", 1.0, 2.0),
]

# Items sold on behalf of third parties: revenue passes through, no margin.
PASSTHROUGH = ["Cigarettes"]

# --- People ---------------------------------------------------------------
# Invented staff. Tax brackets model the Serbian flat-rate contribution
# brackets used for casual/temporary hospitality work.

EMPLOYEES = [
    # name, role, bracket, monthly net salary (0 = paid per shift)
    ("Milica Jovanovic", "Operations manager", "full", 142000),
    ("Stefan Petrovic", "Bar manager", "full", 128000),
    ("Ana Nikolic", "Accountant (part-time)", "full", 86000),
    ("Marko Ilic", "Head bartender", "full", 104000),
    ("Jelena Stankovic", "Bartender", "casual_a", 0),
    ("Nikola Savic", "Bartender", "casual_a", 0),
    ("Teodora Markovic", "Bartender", "casual_a", 0),
    ("Luka Djordjevic", "Waiter", "casual_a", 0),
    ("Sara Pavlovic", "Waiter", "casual_a", 0),
    ("Lazar Ilic", "Waiter", "casual_b", 0),
    ("Milan Ristic", "Security", "casual_b", 0),
    ("Vuk Simic", "Security", "casual_b", 0),
    ("Katarina Tomic", "Host", "casual_a", 0),
    ("Dusan Zivkovic", "Technician", "casual_b", 0),
    ("Ivana Radic", "Cleaner", "casual_a", 0),
    ("Bojan Kostic", "Runner", "casual_b", 0),
]

# Gross cost multiplier applied to net pay, by bracket.
BRACKET_MULTIPLIER = {"full": 1.63, "casual_a": 1.28, "casual_b": 1.21, "unknown": 1.30}

# --- Suppliers ------------------------------------------------------------
# (name, PIB, category, typical net invoice amount, VAT rate, cadence/month)

SUPPLIERS = [
    ("Balkan Beverage Distribution d.o.o.", "101000011", "Drinks", 340000, 20, 4),
    ("Hop & Malt Trade d.o.o.", "101000012", "Drinks", 190000, 20, 3),
    ("Vinarija Tri Brega d.o.o.", "101000013", "Drinks", 86000, 20, 2),
    ("Fresh Produce Market d.o.o.", "101000014", "Food", 64000, 10, 6),
    ("Gastro Supply Partner d.o.o.", "101000015", "Food", 148000, 10, 4),
    ("City Utilities JP", "101000016", "Utilities", 210000, 20, 1),
    ("Waterworks JKP", "101000017", "Utilities", 38000, 10, 1),
    ("Northside Property Holding d.o.o.", "101000018", "Rent", 480000, 20, 1),
    ("Ledger & Books Agency d.o.o.", "101000019", "Professional", 52000, 20, 1),
    ("CleanSweep Services d.o.o.", "101000020", "Services", 44000, 20, 1),
    ("StagePro Equipment Rental d.o.o.", "101000021", "Equipment", 96000, 20, 2),
    ("Northlight Sound & Light d.o.o.", "101000022", "Equipment", 72000, 20, 2),
    ("Mediagraph Agency d.o.o.", "101000023", "Marketing", 68000, 20, 2),
    ("SocialReach Studio d.o.o.", "101000024", "Marketing", 45000, 20, 1),
    ("POS Systems Provider d.o.o.", "101000025", "IT", 28000, 20, 1),
    ("Secure Entry d.o.o.", "101000026", "Services", 36000, 20, 1),
    ("Packaging Direct d.o.o.", "101000027", "Consumables", 31000, 20, 2),
    ("Ice & Cold Logistics d.o.o.", "101000028", "Consumables", 24000, 20, 3),
    ("Signage Works d.o.o.", "101000029", "Services", 19000, 20, 1),
    ("Audio Royalties Association", "101000030", "Royalties", 18000, 20, 1),
    ("Performers Rights Collective", "101000031", "Royalties", 12000, 20, 1),
]

# Outbound invoice counterparties: event organisers, sponsors, tenants.
CLIENTS = [
    ("Festival Collective Udruzenje", "102000011", "Event organiser", 420000, 20),
    ("Northern Promotions d.o.o.", "102000012", "Event organiser", 310000, 20),
    ("Riverside Cultural Fund", "102000013", "Event organiser", 260000, 20),
    ("Brightline Beverages d.o.o.", "102000014", "Sponsor", 540000, 20),
    ("TelCo Connect a.d.", "102000015", "Sponsor", 680000, 20),
    ("Urban Mobility d.o.o.", "102000016", "Sponsor", 290000, 20),
    ("Corner Kiosk d.o.o.", "102000017", "Sublease", 95000, 20),
    ("Parking Operator d.o.o.", "102000018", "Parking", 175000, 20),
    ("Rooftop Film Club", "102000021", "Other", 60000, 20),
    ("Creative Agency Loop d.o.o.", "102000019", "Venue rental", 230000, 20),
    ("Tech Meetup Association", "102000020", "Venue rental", 140000, 20),
]

# --- Events ---------------------------------------------------------------

EVENT_TYPES = [
    "Concert",
    "DJ night",
    "Festival day",
    "Private rental",
    "Theatre",
    "Film screening",
    "Market day",
    "Conference",
    "Regular day",
]

EVENT_NAME_PARTS = {
    "Concert": ["Acoustic Session", "Brass Night", "Indie Showcase", "Jazz Quartet",
                "Rock Revival", "Singer-Songwriter Night", "Balkan Strings"],
    "DJ night": ["Deep House Set", "Disco Revival", "Techno Basement", "Afrobeat Night",
                 "Vinyl Only", "Sunset Session"],
    "Festival day": ["Summer Festival Day 1", "Summer Festival Day 2",
                     "Summer Festival Day 3", "Autumn Festival Day 1"],
    "Private rental": ["Corporate Evening", "Birthday Celebration", "Wedding Reception",
                       "Product Launch", "Team Offsite"],
    "Theatre": ["Monodrama Evening", "Improv Night", "Street Theatre"],
    "Film screening": ["Open-air Cinema", "Documentary Night", "Short Film Selection"],
    "Market day": ["Design Market", "Vintage Market", "Farmers Market"],
    "Conference": ["Tech Talks", "Creative Industries Forum", "Startup Pitch Day"],
    "Regular day": ["Regular operations"],
}

ORGANISERS = [
    "In-house",
    "Festival Collective Udruzenje",
    "Northern Promotions d.o.o.",
    "Riverside Cultural Fund",
    "Creative Agency Loop d.o.o.",
    "Tech Meetup Association",
]

# --- Fixed cost tags ------------------------------------------------------
# (name, type, submodule, venue)

FIXED_COST_TAGS = [
    ("Rent", "expense", "operativni", "shared"),
    ("Electricity", "expense", "operativni", "shared"),
    ("Water", "expense", "operativni", "shared"),
    ("Waste collection", "expense", "operativni", "shared"),
    ("Internet & telecom", "expense", "operativni", "shared"),
    ("POS licence", "expense", "operativni", "shared"),
    ("Bookkeeping", "expense", "operativni", "shared"),
    ("Cleaning", "expense", "operativni", "Garden"),
    ("Security", "expense", "operativni", "Garden"),
    ("Music royalties", "expense", "operativni", "shared"),
    ("Insurance", "expense", "operativni", "shared"),
    ("Bank charges", "expense", "operativni", "shared"),
    ("Marketing retainer", "expense", "operativni", "shared"),
    ("Equipment maintenance", "expense", "operativni", "Garden"),
    ("Parking ramp service", "expense", "operativni", "shared"),
    ("Parking income", "income", "parking", "shared"),
    ("Sublease income", "income", "podnajam", "Corner"),
    ("Sponsorship income", "income", "sponzori", "shared"),
    ("Other income", "income", "ostalo", "shared"),
]

# --- Web analytics --------------------------------------------------------

DEMO_SITE_TAG = "demo0000000000000000000000000000"
DEMO_SITE_HOST = "demo-venue.example"

ANALYTICS_COUNTRIES = [
    ("RS", 0.62), ("DE", 0.07), ("AT", 0.05), ("HR", 0.04), ("US", 0.04),
    ("BA", 0.03), ("ME", 0.03), ("SI", 0.03), ("IT", 0.02), ("FR", 0.02),
    ("GB", 0.02), ("NL", 0.01), ("other", 0.02),
]

ANALYTICS_REFERRERS = [
    ("(direct)", 0.46), ("instagram.com", 0.21), ("google.com", 0.16),
    ("facebook.com", 0.06), ("t.co", 0.03), ("linkedin.com", 0.02),
    ("news-portal.example", 0.03), ("event-listing.example", 0.03),
]

ANALYTICS_PATHS = [
    ("/", 0.34), ("/program", 0.22), ("/events", 0.14), ("/about", 0.08),
    ("/contact", 0.07), ("/gallery", 0.06), ("/venue-rental", 0.05),
    ("/news", 0.04),
]

ANALYTICS_DEVICES = [("mobile", 0.71), ("desktop", 0.24), ("tablet", 0.05)]
