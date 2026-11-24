"""Generate data/sample_listings.csv: messy, realistic listings with duplicates.

Run from the repo root:  python scripts/generate_sample.py
The output is deterministic (fixed random seed).
"""
import csv
import os
import random

# (brand, product, sizes, descriptions)
CATALOGUE = [
    ("Amul", "butter", ["100g", "500g"], ["pck of 2", "pasteurised table butter", ""]),
    ("Amul", "cheese slices", ["200g", "400g"], ["processed cheese, 10 slices", ""]),
    ("Amul", "taaza toned milk", ["500ml", "1L"], ["tetra pack", "long life milk"]),
    ("Mother Dairy", "paneer", ["200g", "400g"], ["fresh malai paneer", ""]),
    ("Aashirvaad", "whole wheat atta", ["1kg", "5kg", "10kg"], ["100% MP sharbati wheat", ""]),
    ("India Gate", "basmati rice", ["1kg", "5kg"], ["classic long grain", "aged 2 yrs"]),
    ("Tata", "salt", ["1kg"], ["iodised, vacuum evaporated", ""]),
    ("Tata", "sampann toor dal", ["500g", "1kg"], ["unpolished", ""]),
    ("Fortune", "sunflower oil", ["1L", "5L"], ["refined, pouch", "jar"]),
    ("Saffola", "gold cooking oil", ["1L", "2L"], ["blended rice bran oil", ""]),
    ("Maggi", "2-minute masala noodles", ["70g", "280g", "560g"], ["pack of 4", "pack of 12", ""]),
    ("Britannia", "good day cashew biscuits", ["75g", "200g"], ["butter cookies", ""]),
    ("Parle", "g glucose biscuits", ["80g", "800g"], ["family pack", ""]),
    ("Lay's", "classic salted chips", ["52g", "90g"], ["potato chips", "party pack"]),
    ("Haldiram's", "aloo bhujia", ["200g", "400g", "1kg"], ["namkeen", ""]),
    ("Kissan", "mixed fruit jam", ["200g", "500g"], ["glass jar", ""]),
    ("Cadbury", "dairy milk chocolate", ["50g", "110g"], ["silk", ""]),
    ("Dabur", "honey", ["250g", "500g"], ["100% pure, squeezy pack", ""]),
    ("MDH", "garam masala", ["50g", "100g"], ["spice blend", ""]),
    ("Everest", "turmeric powder", ["100g", "200g"], ["haldi", ""]),
    ("Tata", "tea gold", ["250g", "500g", "1kg"], ["leaf tea", "15% long leaves"]),
    ("Nescafe", "classic instant coffee", ["50g", "100g", "200g"], ["glass jar", "pouch"]),
    ("Bru", "instant coffee", ["50g", "100g"], ["coffee chicory mix", ""]),
    ("Coca-Cola", "soft drink", ["750ml", "1.25L", "2L"], ["pet bottle", ""]),
    ("Sprite", "lemon lime drink", ["750ml", "2L"], ["pet bottle", "chilled"]),
    ("Tropicana", "orange juice", ["200ml", "1L"], ["100% juice, no added sugar", ""]),
    ("Bisleri", "packaged drinking water", ["1L", "5L"], ["pack of 12", ""]),
    ("Red Bull", "energy drink", ["250ml"], ["can", "pack of 4"]),
    ("Dove", "cream beauty bathing soap", ["100g", "125g"], ["pack of 3", "pack of 5", ""]),
    ("Lifebuoy", "total 10 soap", ["125g"], ["pack of 4", "germ protection"]),
    ("Colgate", "strong teeth toothpaste", ["100g", "200g", "500g"], ["saver pack", ""]),
    ("Pepsodent", "germicheck toothpaste", ["150g", "300g"], ["", "twin pack"]),
    ("Pantene", "hair fall control shampoo", ["180ml", "340ml", "650ml"], ["pro-v", ""]),
    ("Himalaya", "neem face wash", ["100ml", "150ml"], ["purifying", "for oily skin"]),
    ("Nivea", "soft moisturising cream", ["50ml", "200ml"], ["light cream", ""]),
    ("Gillette", "mach3 razor", ["1pc"], ["with 2 cartridges", ""]),
    ("Dettol", "liquid handwash", ["200ml", "750ml"], ["original, refill pouch", "pump"]),
    ("Surf Excel", "easy wash detergent powder", ["1kg", "4kg"], ["", "front + top load"]),
    ("Ariel", "matic liquid detergent", ["1L", "2L"], ["front load", "top load"]),
    ("Vim", "dishwash gel lemon", ["250ml", "750ml"], ["bottle", ""]),
    ("Lizol", "floor cleaner citrus", ["500ml", "975ml"], ["disinfectant surface cleaner", ""]),
    ("Harpic", "toilet cleaner", ["500ml", "1L"], ["power plus", "pack of 2"]),
    ("Good Knight", "mosquito repellent refill", ["45ml"], ["pack of 2", ""]),
    ("Odonil", "room freshener block", ["50g", "75g"], ["lavender", "jasmine"]),
    ("Scotch-Brite", "scrub pad", ["3pcs"], ["large, green", ""]),
    ("boAt", "bassheads 100 wired earphones", [""], ["with mic, black", "with mic, red"]),
    ("boAt", "rockerz 255 bluetooth earphones", [""], ["neckband, 10hr battery", ""]),
    ("Philips", "led bulb", ["9W", "12W"], ["b22, cool day light", "pack of 2"]),
    ("Syska", "powerbank", ["10000mAh", "20000mAh"], ["fast charging", ""]),
    ("Duracell", "aa alkaline batteries", [""], ["pack of 4", "pack of 10"]),
    ("Portronics", "usb type c charger cable", ["1m", "2m"], ["nylon braided", ""]),
    ("Samsung", "25W travel adapter", [""], ["type c, without cable", ""]),
    ("Milton", "thermosteel flask", ["500ml", "1L"], ["24 hr hot & cold", ""]),
    ("Prestige", "popular pressure cooker", ["3L", "5L"], ["aluminium, outer lid", ""]),
    ("Pigeon", "non-stick tawa", ["25cm", "28cm"], ["induction base", ""]),
    ("Cello", "steel lunchbox", ["750ml"], ["3 container set", ""]),
    ("Borosil", "glass mixing bowl", ["500ml", "1.3L"], ["microwave safe", "set of 2"]),
    ("Puma", "mens running shoes", [""], ["size 8, black", "size 9, navy"]),
    ("Nike", "sports cap", [""], ["adjustable, unisex", ""]),
    ("Levi's", "511 slim fit jeans", [""], ["mens, waist 32", "mens, waist 34"]),
    ("Jockey", "cotton ankle socks", [""], ["pack of 3", "pack of 5"]),
    ("Bata", "leather belt", [""], ["mens, brown", "mens, black"]),
    ("Adidas", "round neck tshirt", [""], ["mens, size M", "mens, size L"]),
    ("Wildcraft", "rain jacket", [""], ["unisex, size L", ""]),
    ("", "loose toor dal", ["500g", "1kg"], ["", "local"]),
    ("", "farm fresh brown eggs", ["6pcs", "12pcs"], ["", "tray"]),
    ("", "steel kadai with lid", ["2L"], ["heavy bottom", ""]),
    ("", "gift voucher", [""], ["Rs 500", "Rs 1000"]),
    ("", "birthday candles", [""], ["pack of 24", ""]),
]

UNIQUE_ROWS = 210
DUPLICATE_ROWS = 40


def mess(text, rng):
    """Change only case and whitespace, the way real seller listings vary."""
    if not text:
        return text
    style = rng.choice(["upper", "lower", "title", "mixed", "asis"])
    if style == "upper":
        text = text.upper()
    elif style == "lower":
        text = text.lower()
    elif style == "title":
        text = text.title()
    elif style == "mixed":
        text = " ".join(w.upper() if rng.random() < 0.4 else w for w in text.split())
    words = text.split()
    text = "".join(w + (" " * rng.choice([1, 1, 1, 2, 3])) for w in words).rstrip()
    if rng.random() < 0.3:
        text = " " + text
    if rng.random() < 0.2:
        text += "  "
    return text


def main():
    rng = random.Random(42)
    combos = []
    for brand, product, sizes, descriptions in CATALOGUE:
        for size in sizes:
            for description in descriptions:
                title = " ".join(part for part in (brand, product, size) if part)
                combos.append((title, description))
    rng.shuffle(combos)
    uniques = combos[:UNIQUE_ROWS]

    rows = [(mess(title, rng), mess(description, rng)) for title, description in uniques]
    # Duplicates: the same content as an earlier row, typed slightly differently.
    for title, description in rng.sample(uniques, DUPLICATE_ROWS):
        rows.append((mess(title, rng), mess(description, rng)))
    rng.shuffle(rows)

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "sample_listings.csv")
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sku", "raw_title", "raw_description"])
        for index, (title, description) in enumerate(rows, start=1):
            writer.writerow(["SKU-%04d" % index, title, description])
    print("Wrote %d rows (%d unique listings, %d duplicates) to %s" % (
        len(rows), len(uniques), DUPLICATE_ROWS, path))


if __name__ == "__main__":
    main()
