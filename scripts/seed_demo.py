"""Seed demo catalogue: 3 top categories (Kids, Mens, Womens) + sub-categories
and 20 products covering every field permutation (discount types, colour
variants, size systems, stock levels, ratings, brands, active/inactive).

Images are intentionally left empty — add them from the admin panel.
Idempotent: re-running skips categories/products that already exist (by slug/title).

Run:  cd backend && python -m scripts.seed_demo
"""
import asyncio
from datetime import datetime, timezone

from app.db.mongodb import connect_to_mongo, get_db


def now():
    return datetime.now(timezone.utc)


def col(name, hex_, stock):
    """A colour variant (images added later from admin)."""
    return {"name": name, "hex": hex_, "images": [], "stock": stock}


async def main():
    await connect_to_mongo()
    db = get_db()

    async def cat(name, slug, parent=None, order=0):
        ex = await db.categories.find_one({"slug": slug})
        if ex:
            return str(ex["_id"])
        res = await db.categories.insert_one(
            {"name": name, "slug": slug, "parent_id": parent, "image": None, "order": order}
        )
        return str(res.inserted_id)

    # ── Top-level categories ────────────────────────────────────────────────
    kids = await cat("Kids", "kids", None, 0)
    mens = await cat("Mens", "mens", None, 1)
    womens = await cat("Womens", "womens", None, 2)

    # ── Sub-categories ──────────────────────────────────────────────────────
    kids_boys = await cat("Boys Clothing", "kids-boys", kids, 0)
    kids_girls = await cat("Girls Clothing", "kids-girls", kids, 1)
    kids_shoes = await cat("Kids Footwear", "kids-footwear", kids, 2)

    mens_tees = await cat("T-Shirts", "mens-tshirts", mens, 0)
    mens_jeans = await cat("Jeans", "mens-jeans", mens, 1)
    mens_shoes = await cat("Footwear", "mens-footwear", mens, 2)

    wom_kurti = await cat("Kurtis", "womens-kurtis", womens, 0)
    wom_saree = await cat("Sarees", "womens-sarees", womens, 1)
    wom_dress = await cat("Dresses", "womens-dresses", womens, 2)

    # ── 20 products, every permutation ──────────────────────────────────────
    # Fields: category_id, mrp, price, discount_pct, discount_on, colors, sizes,
    # stock (only when no colour variants), rating, review_count, sold_count, is_active.
    products = [
        # KIDS · Boys
        {"title": "Boys Printed T-Shirt", "brand": "H&M Kids", "category_id": kids_boys,
         "description": "Soft cotton round-neck tee with fun prints.",
         "mrp": 799, "price": 499, "sizes": ["2-3Y", "4-5Y", "6-7Y"],
         "colors": [col("Red", "#E23744", 12), col("Blue", "#2F6BFF", 8), col("Yellow", "#F5C518", 0)],
         "rating": 4.3, "review_count": 120, "sold_count": 640},

        {"title": "Boys Denim Shorts", "brand": "U.S. Polo Assn.", "category_id": kids_boys,
         "description": "Durable denim shorts with adjustable waist.",
         "mrp": 1299, "price": 899, "discount_pct": 10, "discount_on": "price",
         "sizes": ["3-4Y", "5-6Y", "7-8Y"], "colors": [col("Navy", "#1F2A44", 15)],
         "rating": 4.0, "review_count": 45, "sold_count": 210},

        # KIDS · Girls
        {"title": "Girls Party Frock", "brand": "Cutecumber", "category_id": kids_girls,
         "description": "Flared party frock with sequin detailing.",
         "mrp": 2499, "price": 1799, "sizes": ["2-3Y", "4-5Y"],
         "colors": [col("Pink", "#FF6FA3", 6), col("Maroon", "#7B1F2B", 3)],
         "rating": 4.7, "review_count": 300, "sold_count": 1500},

        {"title": "Girls Legging Pack of 3", "brand": "Max Kids", "category_id": kids_girls,
         "description": "Everyday stretch leggings, pack of three.",
         "mrp": 699, "price": 699, "sizes": ["S", "M", "L"], "colors": [], "stock": 40,
         "rating": 0, "review_count": 0, "sold_count": 0},  # brand-new, no reviews, no discount

        # KIDS · Footwear
        {"title": "Kids Sports Shoes", "brand": "Campus", "category_id": kids_shoes,
         "description": "Lightweight everyday sports shoes with velcro strap.",
         "mrp": 1499, "price": 1199, "discount_pct": 20, "discount_on": "mrp",
         "sizes": ["10", "11", "12", "13"],
         "colors": [col("White", "#FFFFFF", 10), col("Black", "#1C1613", 5)],
         "rating": 4.2, "review_count": 88, "sold_count": 500},

        {"title": "Kids Casual Sandals", "brand": "Bata", "category_id": kids_shoes,
         "description": "Comfy summer sandals for everyday wear.",
         "mrp": 599, "price": 449, "sizes": ["8", "9", "10"],
         "colors": [col("Brown", "#7A4B2B", 0), col("Blue", "#2F6BFF", 0)],  # all out of stock
         "rating": 2.9, "review_count": 22, "sold_count": 60},

        # MENS · T-Shirts
        {"title": "Men's Cotton Crew Tee", "brand": "Levi's", "category_id": mens_tees,
         "description": "Classic 100% cotton crew-neck t-shirt.",
         "mrp": 1199, "price": 799, "sizes": ["S", "M", "L", "XL", "XXL"],
         "colors": [col("Black", "#1C1613", 25), col("White", "#FFFFFF", 30),
                    col("Navy", "#1F2A44", 12), col("Olive", "#708238", 5)],
         "rating": 4.6, "review_count": 1500, "sold_count": 35000},  # best-seller

        {"title": "Men's Oversized Tee", "brand": "Bewakoof", "category_id": mens_tees,
         "description": "Drop-shoulder oversized fit tee.",
         "mrp": 999, "price": 399, "sizes": ["M", "L", "XL"],
         "colors": [col("Beige", "#E8DCC8", 18), col("Black", "#1C1613", 20)],
         "rating": 4.1, "review_count": 640, "sold_count": 4200},  # ~60% off, discount filter

        {"title": "Men's Polo T-Shirt", "brand": "U.S. Polo Assn.", "category_id": mens_tees,
         "description": "Pique-knit polo with tipped collar.",
         "mrp": 1799, "price": 1799, "discount_pct": 15, "discount_on": "price",
         "sizes": ["M", "L", "XL"],
         "colors": [col("Maroon", "#7B1F2B", 10), col("Navy", "#1F2A44", 8)],
         "rating": 4.4, "review_count": 210, "sold_count": 800},  # discount via pct, mrp==price

        # MENS · Jeans
        {"title": "Men's Slim Fit Jeans", "brand": "Wrangler", "category_id": mens_jeans,
         "description": "Stretchable slim-fit denim.",
         "mrp": 2999, "price": 1999, "sizes": ["28", "30", "32", "34", "36"],
         "colors": [col("Blue", "#2F6BFF", 14), col("Black", "#1C1613", 9), col("Grey", "#9AA0A6", 4)],
         "rating": 4.5, "review_count": 900, "sold_count": 5600},

        {"title": "Men's Distressed Jeans", "brand": "Spykar", "category_id": mens_jeans,
         "description": "Ripped-knee tapered jeans.",
         "mrp": 3499, "price": 2499, "discount_pct": 10, "discount_on": "mrp",
         "sizes": ["30", "32", "34"], "colors": [col("Blue", "#3B5998", 3)],  # low stock
         "rating": 4.2, "review_count": 130, "sold_count": 340},

        {"title": "Men's Cargo Joggers", "brand": "HRX", "category_id": mens_jeans,
         "description": "Utility cargo joggers with side pockets.",
         "mrp": 2199, "price": 1499, "sizes": ["S", "M", "L", "XL"], "colors": [], "stock": 0,
         "rating": 4.0, "review_count": 75, "sold_count": 250},  # out of stock, no variants

        # MENS · Footwear
        {"title": "Men's Running Sneakers", "brand": "New Balance", "category_id": mens_shoes,
         "description": "Cushioned running sneakers for daily miles.",
         "mrp": 5999, "price": 3999, "sizes": ["7", "8", "9", "10", "11"],
         "colors": [col("Orange", "#F26A21", 8), col("Black", "#1C1613", 12), col("Blue", "#2F6BFF", 6)],
         "rating": 4.8, "review_count": 2100, "sold_count": 12000},  # premium, popular

        {"title": "Men's Formal Derby Shoes", "brand": "Hush Puppies", "category_id": mens_shoes,
         "description": "Genuine leather derby formal shoes.",
         "mrp": 4499, "price": 3199, "discount_pct": 5, "discount_on": "price",
         "sizes": ["7", "8", "9", "10"],
         "colors": [col("Brown", "#7A4B2B", 7), col("Black", "#1C1613", 9)],
         "rating": 4.3, "review_count": 180, "sold_count": 520},

        {"title": "Men's Flip Flops", "brand": "Adidas", "category_id": mens_shoes,
         "description": "Everyday cushioned flip flops.",
         "mrp": 899, "price": 599, "sizes": ["Free Size"], "colors": [col("Black", "#1C1613", 40)],
         "rating": 3.9, "review_count": 340, "sold_count": 2600},  # cheap, Free Size

        # WOMENS · Kurtis
        {"title": "Women's Anarkali Kurti", "brand": "Libas", "category_id": wom_kurti,
         "description": "Floor-length Anarkali with intricate print.",
         "mrp": 2299, "price": 1299, "sizes": ["S", "M", "L", "XL", "XXL"],
         "colors": [col("Teal", "#0E8F8F", 10), col("Maroon", "#7B1F2B", 6), col("Mustard", "#D4A017", 4)],
         "rating": 4.6, "review_count": 780, "sold_count": 3400},

        {"title": "Women's Straight Cotton Kurti", "brand": "W", "category_id": wom_kurti,
         "description": "Breathable straight-cut cotton kurti.",
         "mrp": 1599, "price": 1599, "sizes": ["M", "L"], "colors": [col("Pink", "#FF6FA3", 12)],
         "rating": 4.1, "review_count": 95, "sold_count": 300},  # no discount

        # WOMENS · Sarees
        {"title": "Banarasi Silk Saree", "brand": "Kalki Fashion", "category_id": wom_saree,
         "description": "Handwoven Banarasi silk with zari border.",
         "mrp": 8999, "price": 5999, "sizes": ["Free Size"],
         "colors": [col("Red", "#E23744", 5), col("Gold", "#D4AF37", 3), col("Green", "#3BB54A", 2)],
         "rating": 4.9, "review_count": 420, "sold_count": 900},  # premium, top rated

        {"title": "Georgette Printed Saree", "brand": "Soch", "category_id": wom_saree,
         "description": "Lightweight georgette saree with floral print.",
         "mrp": 2499, "price": 1499, "discount_pct": 20, "discount_on": "mrp",
         "sizes": ["Free Size"],
         "colors": [col("Blue", "#2F6BFF", 0), col("Pink", "#FF6FA3", 0)],  # out of stock, low rating
         "rating": 3.5, "review_count": 40, "sold_count": 120},

        # WOMENS · Dresses  (inactive — demo of hidden/draft state, toggle on in admin)
        {"title": "Women's Bodycon Dress", "brand": "Zara", "category_id": wom_dress,
         "description": "Ribbed bodycon midi dress. (Set inactive as a draft demo.)",
         "mrp": 3499, "price": 2799, "sizes": ["XS", "S", "M", "L"],
         "colors": [col("Black", "#1C1613", 15), col("Red", "#E23744", 8)],
         "rating": 4.4, "review_count": 560, "sold_count": 2100, "is_active": False},
    ]

    inserted = 0
    for p in products:
        if await db.products.find_one({"title": p["title"]}):
            continue
        p.setdefault("images", [])
        p.setdefault("discount_pct", 0)
        p.setdefault("discount_on", "price")
        p.setdefault("colors", [])
        p.setdefault("sizes", [])
        p.setdefault("stock", 0)
        p.setdefault("low_stock_threshold", 5)
        p.setdefault("rating", 0)
        p.setdefault("review_count", 0)
        p.setdefault("sold_count", 0)
        p.setdefault("is_active", True)
        p["created_at"] = now()
        await db.products.insert_one(p)
        inserted += 1

    total = await db.products.count_documents({})
    cats = await db.categories.count_documents({})
    print(f"Inserted {inserted} new products. Catalogue now has {total} products, {cats} categories.")
    print("Done. Add images from the admin panel.")


if __name__ == "__main__":
    asyncio.run(main())
