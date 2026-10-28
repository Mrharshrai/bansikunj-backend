"""Populate the database with the shop's catalogue.

Run with::

    .\\.venv\\Scripts\\python.exe -m app.scripts.seed_catalogue

Idempotent: categories and products are matched on their slug, so re-running
updates the existing rows instead of duplicating them. Safe to run against a
fresh database or an existing one.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.database import AsyncSessionLocal
from ..models.category import Category
from ..models.product import Product
from ..models.user import Role, User

#: Slugs are written out rather than derived from the name, because the
#: frontend's navigation and the product rows below both refer to them
#: directly (``/category/premium``, ``/category/tubs``). Deriving would give
#: ``premium-cookies`` and break those links.
CATEGORIES: list[dict] = [
    {
        "name": "Soft & Chewy",
        "slug": "soft-chewy",
        "description": "Gooey-centered cookies with a soft, bakery-fresh bite.",
        "display_order": 1,
    },
    {
        "name": "Premium Cookies",
        "slug": "premium",
        "description": "Single-origin chocolate, Dutch-process cocoa, real butter.",
        "display_order": 2,
    },
    {
        "name": "Desserts",
        "slug": "desserts",
        "description": "Choco lava cakes, brownies and macarons baked to order.",
        "display_order": 3,
    },
    {
        "name": "Gifting",
        "slug": "gifts",
        "description": "Gift boxes and festive assortments, packed by hand.",
        "display_order": 4,
    },
    {
        "name": "Cookie Tubs",
        "slug": "tubs",
        "description": "Build your own tub or pick one of our ready mixes.",
        "display_order": 5,
    },
]

PRODUCTS: list[dict] = [
    {
        "name": "Chocolate Chip Classic",
        "image_url": "/assets/images/products/chocolate-chip.jpg",
        "category": "soft-chewy",
        "description": "The one everyone reaches for: crisp-edged, soft-centered, loaded with dark chocolate chunks.",
        "price": Decimal("249.00"),
        "mrp": Decimal("299.00"),
        "stock_quantity": 120,
        "weight": "12 cookies • 480g",
        "tags": ["chocolate", "classic", "bestseller"],
        "is_best_seller": True,
        "rating": Decimal("4.80"),
        "review_count": 214,
    },
    {
        "name": "Triple Chocolate Chunk",
        "image_url": "/assets/images/products/double-chocolate.jpg",
        "category": "premium",
        "description": "Dark, milk and white chocolate in one cookie, with a molten middle.",
        "price": Decimal("329.00"),
        "mrp": Decimal("379.00"),
        "stock_quantity": 80,
        "weight": "12 cookies • 500g",
        "tags": ["chocolate", "dark", "premium"],
        "is_best_seller": True,
        "rating": Decimal("4.90"),
        "review_count": 176,
    },
    {
        "name": "Salted Caramel Cashew",
        "image_url": "/assets/images/products/caramel-cashew.jpg",
        "category": "soft-chewy",
        "description": "Caramel and roasted cashew with a pinch of sea salt on top.",
        "price": Decimal("299.00"),
        "mrp": Decimal("349.00"),
        "stock_quantity": 60,
        "weight": "12 cookies • 460g",
        "tags": ["caramel", "nuts", "bestseller"],
        "is_best_seller": True,
        "rating": Decimal("4.70"),
        "review_count": 143,
    },
    {
        "name": "Belgian Dark Chunk",
        "image_url": "/assets/images/products/belgian-dark.jpg",
        "category": "premium",
        "description": "70% Belgian couverture shards in a crisp, thin-baked cookie.",
        "price": Decimal("349.00"),
        "mrp": None,
        "stock_quantity": 45,
        "weight": "10 cookies • 400g",
        "tags": ["dark", "premium", "chocolate"],
        "rating": Decimal("4.60"),
        "review_count": 88,
    },
    {
        "name": "Choco Lava Cake",
        "image_url": "/assets/images/products/choco-lava-cake.jpg",
        "category": "desserts",
        "description": "Warm chocolate sponge with a molten centre. Ships frozen, bake for 12 minutes.",
        "price": Decimal("399.00"),
        "mrp": Decimal("449.00"),
        "stock_quantity": 30,
        "weight": "2 cakes • 2 × 110g",
        "tags": ["lava", "chocolate", "dessert"],
        "is_best_seller": True,
        "rating": Decimal("4.90"),
        "review_count": 201,
    },
    {
        "name": "Fudgy Brownie",
        "image_url": "/assets/images/products/fudge-brownie.jpg",
        "category": "desserts",
        "description": "Deeply fudgy, barely sweet, with a crackled top.",
        "price": Decimal("279.00"),
        "mrp": None,
        "stock_quantity": 50,
        "weight": "4 brownies • 4 × 90g",
        "tags": ["brownie", "dessert", "chocolate"],
        "rating": Decimal("4.50"),
        "review_count": 97,
    },
    {
        "name": "French Macaron Box",
        "image_url": "/assets/images/products/macaron-box.jpg",
        "category": "gifts",
        "description": "Nine delicate macarons in assorted flavours, boxed for gifting.",
        "price": Decimal("899.00"),
        "mrp": Decimal("999.00"),
        "stock_quantity": 20,
        "weight": "9 macarons",
        "tags": ["macaron", "gift", "festive"],
        "rating": Decimal("4.40"),
        "review_count": 64,
    },
    {
        "name": "Festive Gift Hamper",
        "image_url": "/assets/images/products/gift-box.jpg",
        "category": "gifts",
        "description": "A generous mix of our bestsellers in a reusable festive box.",
        "price": Decimal("1499.00"),
        "mrp": Decimal("1799.00"),
        "stock_quantity": 15,
        "weight": "36 cookies • 1.5kg",
        "tags": ["gift", "festive", "hamper", "premium"],
        "rating": Decimal("5.00"),
        "review_count": 52,
    },
    {
        "name": "Make Your Own Cookie Tub",
        "image_url": "/assets/images/products/cookie-tub.jpg",
        "category": "tubs",
        "description": "Pick your dough, pick your mix-ins, build your very own personalised tub.",
        "price": Decimal("599.00"),
        "mrp": None,
        "stock_quantity": 25,
        "weight": "12 cookies • 480g",
        "tags": ["tub", "custom", "fun"],
        "rating": Decimal("4.80"),
        "review_count": 118,
    },
    {
        "name": "Everyday Cookie Tub",
        "image_url": "/assets/images/products/cookie-tub.jpg",
        "category": "tubs",
        "description": "A daily dose of mixed classics — ideal for desks, classrooms and snack corners.",
        "price": Decimal("449.00"),
        "mrp": Decimal("499.00"),
        "stock_quantity": 40,
        "weight": "16 cookies • 640g",
        "tags": ["tub", "mixed", "value"],
        "is_best_seller": True,
        "rating": Decimal("4.70"),
        "review_count": 220,
    },
    {
        "name": "Oatmeal Raisin",
        "image_url": "/assets/images/products/oatmeal-raisin.jpg",
        "category": "soft-chewy",
        "description": "Chewy oat porridge cookies with plump raisins and a hint of cinnamon.",
        "price": Decimal("259.00"),
        "mrp": None,
        "stock_quantity": 70,
        "weight": "12 cookies • 450g",
        "tags": ["oats", "raisin", "healthy"],
        "rating": Decimal("4.30"),
        "review_count": 76,
    },
    {
        "name": "Salted Peanut Butter",
        "image_url": "/assets/images/products/peanut-butter.jpg",
        "category": "soft-chewy",
        "description": "Roasted peanut butter and a pinch of flaky sea salt.",
        "price": Decimal("289.00"),
        "mrp": None,
        "stock_quantity": 0,
        "weight": "12 cookies • 470g",
        "tags": ["nuts", "peanut", "bestseller"],
        "rating": Decimal("4.50"),
        "review_count": 91,
    },
]

ADMIN_EMAIL = "admin@bansikunj.example.com"
ADMIN_PASSWORD = "AdminPass123"


def slugify(name: str) -> str:
    out = []
    for char in name.lower():
        out.append(char if char.isalnum() else "-")
    return "-".join(part for part in "".join(out).split("-") if part)


async def seed(session: AsyncSession) -> None:
    # Fail loudly on a typo'd category reference rather than part-way through a
    # commit, which would leave the catalogue in a half-applied state.
    known = {spec["slug"] for spec in CATEGORIES}
    unknown = {spec["category"] for spec in PRODUCTS} - known
    if unknown:
        raise ValueError(f"products reference unknown category slug(s): {sorted(unknown)}")

    by_slug: dict[str, Category] = {}
    for spec in CATEGORIES:
        slug = spec["slug"]
        existing = await session.scalar(select(Category).where(Category.slug == slug))
        if existing is None:
            existing = Category(slug=slug)
            session.add(existing)
        for key, value in spec.items():
            setattr(existing, key, value)
        by_slug[slug] = existing
    await session.flush()

    for spec in PRODUCTS:
        slug = slugify(spec["name"])
        payload = dict(spec)
        category = by_slug[payload.pop("category")]

        existing = await session.scalar(select(Product).where(Product.slug == slug))
        if existing is None:
            existing = Product(slug=slug)
            session.add(existing)
        for key, value in payload.items():
            setattr(existing, key, value)
        existing.category_id = category.id

    await session.flush()

    admin = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
    if admin is None:
        from ..core.security import hash_password

        admin = User(
            email=ADMIN_EMAIL,
            full_name="Catalogue Admin",
            hashed_password=hash_password(ADMIN_PASSWORD),
            role=Role.ADMIN,
            is_active=True,
        )
        session.add(admin)
        print(f"created admin {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    else:
        print(f"admin {ADMIN_EMAIL} already exists")


async def main() -> None:
    settings = get_settings()
    print(f"seeding {settings.POSTGRES_DB} on {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}")
    async with AsyncSessionLocal() as session:
        await seed(session)
        await session.commit()
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
