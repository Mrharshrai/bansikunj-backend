"""Current-user profile endpoints. Every route is scoped to the caller."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ...core.dependencies import AuthServiceDep, CurrentUser, DbSessionDep
from ...core.envelope import SuccessEnvelope
from ...core.logging import get_logger
from ...schemas.auth import MessageResponse
from ...schemas.user import PasswordChangeRequest, UserOut, UserProfileUpdate
from ...services.user_service import UserService

logger = get_logger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


def get_user_service(session: DbSessionDep) -> UserService:
    """User service bound to the request's session."""
    return UserService(session)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]


@router.get(
    "/me", response_model=SuccessEnvelope[UserOut], summary="Fetch the current user"
)
async def read_me(user: CurrentUser) -> SuccessEnvelope[UserOut]:
    """Return the authenticated user.

    ``UserOut`` has no ``hashed_password`` field, so the hash is filtered out by
    FastAPI's response validation rather than merely omitted by convention.
    """
    return SuccessEnvelope[UserOut](
        message="Current user", data=UserOut.model_validate(user, from_attributes=True)
    )


@router.put(
    "/me", response_model=SuccessEnvelope[UserOut], summary="Replace editable profile fields"
)
async def replace_me(
    payload: UserProfileUpdate, user: CurrentUser, service: UserServiceDep
) -> SuccessEnvelope[UserOut]:
    updated = await service.update_profile(user, payload)
    logger.info("user.profile_replaced", user_id=str(user.id))
    return SuccessEnvelope[UserOut](
        message="Profile updated successfully",
        data=UserOut.model_validate(updated, from_attributes=True),
    )


@router.patch(
    "/me", response_model=SuccessEnvelope[UserOut], summary="Partially update the profile"
)
async def patch_me(
    payload: UserProfileUpdate, user: CurrentUser, service: UserServiceDep
) -> SuccessEnvelope[UserOut]:
    """Only fields present in the body are changed; the rest are left alone."""
    updated = await service.update_profile(user, payload)
    logger.info("user.profile_patched", user_id=str(user.id))
    return SuccessEnvelope[UserOut](
        message="Profile updated successfully",
        data=UserOut.model_validate(updated, from_attributes=True),
    )


@router.post(
    "/me/change-password",
    response_model=SuccessEnvelope[MessageResponse],
    summary="Change the password",
)
async def change_password(
    payload: PasswordChangeRequest, user: CurrentUser, auth: AuthServiceDep
) -> SuccessEnvelope[MessageResponse]:
    """Change the password and revoke every existing session."""
    await auth.change_password(user, payload.current_password, payload.new_password)
    message = "Password changed successfully"
    return SuccessEnvelope[MessageResponse](
        message=message, data=MessageResponse(message=message)
    )
