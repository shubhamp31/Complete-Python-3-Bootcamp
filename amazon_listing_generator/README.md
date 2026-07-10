# Amazon India Listing Generator — Lukson

A production-grade desktop application that converts a **Shopify product
export (CSV)** into a fully populated **Amazon India bulk listing file
(.xlsm)** — including parent/child variations, Lukson business rules,
SEO content, image mapping and full validation reports.

```
Shopify CSV ──► Read ──► Variations ──► Business Rules ──► Images ──► SEO
                                                                       │
   Amazon Upload File.xlsm ◄── Excel Writer ◄── Validation ◄── Field Mapping
   Validation Report.xlsx
   Error Report.xlsx
   Summary Report.xlsx
```

## Features

- **Shopify reader** — products, variants, prices, inventory, images,
  vendor, tags, HTML descriptions, SEO fields, weights and metafields.
  Handles 20,000+ rows in seconds (fully vectorized pandas).
- **Dynamic template handling** — Amazon template headers are located **by
  name**, never by column number. Macros, drop-downs, data validation,
  hidden sheets and protection are preserved (`openpyxl keep_vba`).
  Both template generations are supported: classic flat files
  (`item_sku`, `feed_product_type`, ...) and the new **Category Listings
  Templates**, whose embedded `settings=` row (label/attribute/data row
  positions) and attribute-path headers
  (`item_name[marketplace_id=...][language_tag=en_IN]#1.value`) are parsed
  automatically. Prefill/example rows in the data area are cleared before
  writing.
- **JSON-driven mapping engine** — every Shopify → Amazon mapping lives in
  `config/field_mapping.json`. New mappings need **zero code changes**.
- **Variation builder** — automatic Parent/Child relationships for Ring
  Size, Metal Purity, Color, Finish and Weight, with correct
  `parent_sku` / `relationship_type` / `variation_theme` values.
- **SEO generator** — ≤200-char titles, 5 benefit bullets, SEO-rich
  descriptions and deduplicated backend search terms within Amazon's byte
  limit.
- **Validation engine** — missing/duplicate SKUs, missing prices, images,
  weights, purity, diamond details, invalid URLs, broken parent/child
  relationships and missing mandatory Amazon fields — with row numbers and
  severity highlighting.
- **Reports** — Validation, Error and Summary workbooks on every run.
- **Never crashes** — all exceptions are caught, logged with stack traces
  to `logs/`, and surfaced as friendly messages.
- **Future-ready** — marketplaces are pluggable via
  `core/marketplace.py`; Flipkart/Myntra/AJIO/Nykaa/Tata CLiQ can be added
  as new generator classes + JSON configs without touching the UI.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Desktop UI
python app.py

# Headless / automation
python app.py --cli sample_data/sample_shopify_export.csv \
              templates/Amazon_Template.xlsm outputs/
```

### Using the app

1. **Browse Shopify CSV** — select your Shopify product export.
2. **Browse Amazon Template** — select the Jewellery `.xlsm` template
   downloaded from Seller Central (see `templates/README.md`).
3. **Browse Output Folder** — choose where the four output files go.
4. **Generate Listings** — watch live progress; when done, use
   **Open Output Folder**.

Outputs: `Amazon Upload File.xlsm`, `Validation Report.xlsx`,
`Error Report.xlsx`, `Summary Report.xlsx`.

## Configuration (no code changes needed)

| File | Purpose |
|---|---|
| `config/field_mapping.json` | Shopify/computed/constant → Amazon field mappings (+ header aliases) |
| `config/defaults.json` | Brand, country of origin, metal type, stone creation method, units, file names |
| `config/amazon_rules.json` | SEO templates & limits, validation rules, variation themes, metafield sources |
| `config/categories.json` | Keyword → feed product type / browse node matching |

Each mapping rule points an Amazon field at one source:

```json
{"amazon_field": "country_of_origin", "source": {"type": "default",  "key": "country_of_origin"}}
{"amazon_field": "item_sku",          "source": {"type": "internal", "column": "_sku"}}
{"amazon_field": "external_product_id","source": {"type": "shopify", "column": "Variant Barcode"}}
{"amazon_field": "warranty_type",     "source": {"type": "constant", "value": "No Warranty"}}
{"amazon_field": "part_number",       "source": {"type": "template", "template": "{_sku}-IN"}}
```

## Lukson business rules (pre-configured)

Brand **Lukson** · Country of Origin **India** · Metal Type **Gold** ·
Metal Stamp from the purity option (9K/14K/18K…) · Stone Creation Method
**Lab Grown** · diamond details from Shopify metafields · weight from
`Variant Grams` · images from Shopify CDN URLs.

## Project layout

```
amazon_listing_generator/
├── app.py                     # entry point (UI + --cli mode)
├── config/                    # all JSON configuration
├── core/                      # marketplace-agnostic engine
│   ├── shopify_reader.py      # CSV → variants + images tables
│   ├── variation_builder.py   # parent/child relationships
│   ├── seo_generator.py       # titles, bullets, descriptions, keywords
│   ├── image_mapper.py        # main + additional image slots
│   ├── mapper.py              # JSON-driven field mapping engine
│   ├── validator.py           # vectorized validation suite
│   ├── amazon_template.py     # dynamic header/layout resolution
│   ├── excel_writer.py        # macro-preserving output + reports
│   ├── pipeline.py            # Amazon India orchestration
│   ├── marketplace.py         # pluggable marketplace registry
│   ├── models.py              # pydantic models
│   └── utils.py               # logging, config, helpers
├── ui/                        # CustomTkinter interface
├── templates/                 # place Amazon_Template.xlsm here
├── sample_data/               # sample export + mock template generator
├── tests/                     # pytest suite
├── outputs/  logs/
└── amazon_listing_generator.spec  # PyInstaller build
```

## Tests

```bash
pytest tests/ -v
```

## Building the executable

```bash
pip install pyinstaller
pyinstaller amazon_listing_generator.spec
# → dist/AmazonListingGenerator(.exe)
```

`config/` and `templates/` are bundled beside the executable and remain
editable after packaging.

## Performance

The pipeline is fully vectorized (pandas/numpy); the only per-cell loop is
the final template write. A 20,000-row export completes well inside the
5-minute budget on a typical laptop.

## Adding a new marketplace

1. Create `core/<marketplace>_pipeline.py` with a class extending
   `MarketplaceGenerator`, decorated with `@register`.
2. Add its JSON mapping/rules files under `config/`.
3. Done — the registry exposes it to the UI and `--marketplace` CLI flag.
