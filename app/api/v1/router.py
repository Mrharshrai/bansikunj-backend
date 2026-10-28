"""Aggregates every v1 router. Mounted by :mod:`app.main` under the API prefix."""
from fastapi import APIRouter

from .addresses import router as addresses_router
from .auth import router as auth_router
from .banners import router as banners_router
from .cart import router as cart_router
from .categories import router as categories_router
from .orders import router as orders_router
from .payments import router as payments_router
from .products import router as products_router
from .users import router as users_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(addresses_router)
api_router.include_router(categories_router)
api_router.include_router(products_router)
api_router.include_router(banners_router)
api_router.include_router(cart_router)
api_router.include_router(orders_router)
api_router.include_router(payments_router)

__all__ = ["api_router"]
