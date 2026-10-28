"""Category endpoints.

Reading is public and only ever returns active categories. Writing requires the
``admin`` role, applied by the ``require_admin`` dependency rather than by
inspecting the user inside the handler.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

from ...models.user import User

from ...core.dependencies import DbSessionDep, OptionalUser, require_admin
from ...core.envelope import SuccessEnvelope
from ...models.user import Role
from ...schemas.category import CategoryCreate, CategoryOut, CategoryUpdate
from ...services.category_service import CategoryService

router = APIRouter(prefix="/categories", tags=["categories"])


def get_category_service(session: DbSessionDep) -> CategoryService:
    return CategoryService(session)


CategoryServiceDep = Annotated[CategoryService, Depends(get_category_service)]

AdminDep = Annotated[User, Depends(require_admin)]


class SingleCategory(BaseModel):
    category: CategoryOut


class CategoryList(BaseModel):
    categories: list[CategoryOut]
    total: int


@router.get("", response_model=SuccessEnvelope[CategoryList], summary="List categories")
async def list_categories(
    service: CategoryServiceDep,
    include_inactive: Annotated[
        bool,
        Query(description="Include hidden categories. Requires the admin role."),
    ] = False,
    viewer: OptionalUser = None,
) -> SuccessEnvelope[CategoryList]:
    """Categories in display order, each with the number of active products.

    ``include_inactive`` only takes effect for an authenticated admin; for anyone
    else the flag is ignored and active categories are returned, so the parameter
    cannot be used to reveal hidden categories.
    """
    is_admin = viewer is not None and viewer.role == Role.ADMIN
    rows = await service.list_categories(include_inactive=include_inactive and is_admin)
    return SuccessEnvelope[CategoryList](
        message="Categories retrieved successfully",
        data=CategoryList(
            categories=[
                CategoryOut.from_entity(category, product_count=count)
                for category, count in rows
            ],
            total=len(rows),
        ),
    )


@router.get(
    "/{slug}",
    response_model=SuccessEnvelope[SingleCategory],
    summary="Fetch one category",
)
async def get_category(
    slug: str, service: CategoryServiceDep
) -> SuccessEnvelope[SingleCategory]:
    category = await service.get_by_slug(slug)
    count = await service.product_count(category)
    return SuccessEnvelope[SingleCategory](
        message="Category retrieved",
        data=SingleCategory(
            category=CategoryOut.from_entity(category, product_count=count)
        ),
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[SingleCategory],
    summary="Create a category",
)
async def create_category(
    payload: CategoryCreate,
    service: CategoryServiceDep,
    _: AdminDep,
) -> SuccessEnvelope[SingleCategory]:
    """Create a category. The slug is derived from the name unless supplied."""
    category = await service.create(payload)
    return SuccessEnvelope[SingleCategory](
        message="Category created successfully",
        data=SingleCategory(category=CategoryOut.from_entity(category)),
    )


@router.put(
    "/{category_id}",
    response_model=SuccessEnvelope[SingleCategory],
    summary="Update a category",
)
async def update_category(
    category_id: str,
    payload: CategoryUpdate,
    service: CategoryServiceDep,
    _: AdminDep,
) -> SuccessEnvelope[SingleCategory]:
    category = await service.update(category_id, payload)
    return SuccessEnvelope[SingleCategory](
        message="Category updated successfully",
        data=SingleCategory(category=CategoryOut.from_entity(category)),
    )


@router.patch(
    "/{category_id}",
    response_model=SuccessEnvelope[SingleCategory],
    summary="Partially update a category",
)
async def patch_category(
    category_id: str,
    payload: CategoryUpdate,
    service: CategoryServiceDep,
    _: AdminDep,
) -> SuccessEnvelope[SingleCategory]:
    """Only fields present in the body are changed."""
    category = await service.update(category_id, payload)
    return SuccessEnvelope[SingleCategory](
        message="Category updated successfully",
        data=SingleCategory(category=CategoryOut.from_entity(category)),
    )


@router.delete(
    "/{category_id}",
    response_model=SuccessEnvelope[None],
    summary="Delete a category",
)
async def delete_category(
    category_id: str, service: CategoryServiceDep, _: AdminDep
) -> SuccessEnvelope[None]:
    """Hard delete. Refused with 409 while any product still references it."""
    await service.delete(category_id)
    return SuccessEnvelope[None](message="Category deleted successfully", data=None)


@router.post(
    "/{category_id}/deactivate",
    response_model=SuccessEnvelope[SingleCategory],
    summary="Hide a category",
)
async def deactivate_category(
    category_id: str,
    service: CategoryServiceDep,
    _: AdminDep,
    force: Annotated[
        bool, Query(description="Hide even if products still reference it.")
    ] = False,
) -> SuccessEnvelope[SingleCategory]:
    """Hide a category without deleting it; use ``force=true`` to override the guard."""
    category = await service.deactivate(category_id, force=force)
    return SuccessEnvelope[SingleCategory](
        message="Category deactivated successfully",
        data=SingleCategory(category=CategoryOut.from_entity(category)),
    )
