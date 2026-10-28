"""Authentication endpoints.

Routes stay thin: validate input, call ``AuthService``, return the standard
envelope. No hashing, token issuance, or role logic lives here.

Responses declare ``response_model=SuccessEnvelope[...]`` so FastAPI both
documents them in OpenAPI *and* filters the payload - a field missing from the
response schema cannot reach a client even if the service returns it.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, status

from ...core.dependencies import AuthServiceDep, CurrentUser
from ...core.envelope import SuccessEnvelope
from ...core.logging import get_logger
from ...schemas.auth import (
    AuthBundle,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenBundle,
    UserSummary,
    VerifyEmailRequest,
)
from ...services.auth_service import NEUTRAL_RESPONSE

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _summary(user: object) -> UserSummary:
    return UserSummary.model_validate(user, from_attributes=True)


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[AuthBundle],
    summary="Create a customer account",
)
async def register(
    payload: RegisterRequest, request: Request, auth: AuthServiceDep
) -> SuccessEnvelope[AuthBundle]:
    """Register a new customer.

    The role is always ``customer`` regardless of what the request body contains.
    """
    result = await auth.register(payload)
    logger.info(
        "auth.register_request", user_id=str(result.user.id), client_ip=_client_ip(request)
    )
    return SuccessEnvelope[AuthBundle](
        message="Account created successfully",
        data=AuthBundle(user=_summary(result.user), tokens=result.tokens.pair),
    )


@router.post(
    "/login", response_model=SuccessEnvelope[AuthBundle], summary="Log in"
)
async def login(
    payload: LoginRequest, request: Request, auth: AuthServiceDep
) -> SuccessEnvelope[AuthBundle]:
    user, tokens = await auth.authenticate(str(payload.email), payload.password)
    logger.info("auth.login_request", user_id=str(user.id), client_ip=_client_ip(request))
    return SuccessEnvelope[AuthBundle](
        message="Logged in successfully",
        data=AuthBundle(user=_summary(user), tokens=tokens.pair),
    )


@router.post(
    "/refresh", response_model=SuccessEnvelope[TokenBundle], summary="Rotate a refresh token"
)
async def refresh(
    payload: RefreshRequest, auth: AuthServiceDep
) -> SuccessEnvelope[TokenBundle]:
    """Exchange a refresh token for a fresh pair.

    The presented token is revoked and replaced, so a replay is detected and
    treated as theft.
    """
    tokens = await auth.refresh(payload.refresh_token)
    return SuccessEnvelope[TokenBundle](
        message="Token refreshed successfully",
        data=TokenBundle(tokens=tokens.pair),
    )


@router.post(
    "/logout",
    response_model=SuccessEnvelope[MessageResponse],
    summary="Revoke the current refresh token",
)
async def logout(payload: RefreshRequest, auth: AuthServiceDep) -> SuccessEnvelope[MessageResponse]:
    await auth.logout(payload.refresh_token)
    return SuccessEnvelope[MessageResponse](
        message="Logged out successfully", data=MessageResponse(message="Logged out successfully")
    )


@router.post(
    "/logout-all",
    response_model=SuccessEnvelope[dict],
    summary="Revoke every session for the current user",
)
async def logout_all(user: CurrentUser, auth: AuthServiceDep) -> SuccessEnvelope[dict]:
    revoked = await auth.logout_everywhere(user.id)
    return SuccessEnvelope[dict](
        message="Logged out from all devices", data={"sessions_revoked": revoked}
    )


@router.post(
    "/forgot-password",
    response_model=SuccessEnvelope[MessageResponse],
    summary="Request a password-reset link",
    description=(
        "Always responds with the same message, whether or not the email is "
        "registered, so the endpoint cannot be used to enumerate accounts."
    ),
)
async def forgot_password(
    payload: ForgotPasswordRequest, auth: AuthServiceDep
) -> SuccessEnvelope[MessageResponse]:
    issued = await auth.request_password_reset(str(payload.email))
    if issued.user is not None:
        logger.info(
            "auth.password_reset_issued",
            user_id=str(issued.user.id),
            has_token=issued.raw_token is not None,
        )
    return SuccessEnvelope[MessageResponse](
        message=NEUTRAL_RESPONSE, data=MessageResponse(message=NEUTRAL_RESPONSE)
    )


@router.post(
    "/reset-password",
    response_model=SuccessEnvelope[MessageResponse],
    summary="Complete a password reset",
)
async def reset_password(
    payload: ResetPasswordRequest, auth: AuthServiceDep
) -> SuccessEnvelope[MessageResponse]:
    await auth.reset_password(payload.token, payload.new_password)
    return SuccessEnvelope[MessageResponse](
        message="Password reset successfully",
        data=MessageResponse(message="Password reset successfully"),
    )


@router.post(
    "/verify-email",
    response_model=SuccessEnvelope[MessageResponse],
    summary="Confirm an email address",
)
async def verify_email(
    payload: VerifyEmailRequest, auth: AuthServiceDep
) -> SuccessEnvelope[MessageResponse]:
    await auth.verify_email(payload.token)
    return SuccessEnvelope[MessageResponse](
        message="Email verified successfully",
        data=MessageResponse(message="Email verified successfully"),
    )


@router.post(
    "/resend-verification",
    response_model=SuccessEnvelope[MessageResponse],
    summary="Send a new email-verification token",
)
async def resend_verification(
    user: CurrentUser, auth: AuthServiceDep
) -> SuccessEnvelope[MessageResponse]:
    if user.is_email_verified:
        message = "Email is already verified"
        return SuccessEnvelope[MessageResponse](
            message=message, data=MessageResponse(message=message)
        )
    await auth.request_email_verification(user)
    logger.info("auth.verification_token_reissued", user_id=str(user.id))
    return SuccessEnvelope[MessageResponse](
        message="Verification email sent",
        data=MessageResponse(message="Verification email sent"),
    )


@router.get(
    "/me", response_model=SuccessEnvelope[UserSummary], summary="Current authenticated user"
)
async def me(user: CurrentUser) -> SuccessEnvelope[UserSummary]:
    return SuccessEnvelope[UserSummary](message="Current user", data=_summary(user))
