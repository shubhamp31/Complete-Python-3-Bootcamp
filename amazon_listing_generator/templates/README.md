# Amazon Template Folder

Place your Amazon India bulk listing template here, e.g. `Amazon_Template.xlsm`.

Download the official **Jewellery** category template from
Seller Central → Catalogue → *Add Products via Upload* → *Download an Inventory File*.

The application reads the template's headers **by name** (never by column
position), and preserves its macros, drop-downs, data validation, hidden
sheets and protection when writing the upload file.

For demos and automated tests you can generate a structurally faithful
mock template with:

```bash
python sample_data/make_sample_template.py templates/Amazon_Template_Sample.xlsx
```
