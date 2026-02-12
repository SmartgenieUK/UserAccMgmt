from __future__ import annotations

from email.message import EmailMessage
import aiosmtplib

from app.core.config import Settings


class EmailService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def send_email(self, to_email: str, subject: str, text_body: str, html_body: str | None = None) -> None:
        message = EmailMessage()
        message["From"] = self.settings.EMAIL_FROM
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(text_body)
        if html_body:
            message.add_alternative(html_body, subtype="html")

        await aiosmtplib.send(
            message,
            hostname=self.settings.SMTP_HOST,
            port=self.settings.SMTP_PORT,
            username=self.settings.SMTP_USER,
            password=self.settings.SMTP_PASSWORD,
            start_tls=self.settings.SMTP_USE_TLS,
        )

    async def send_verification_email(self, to_email: str, token: str) -> None:
        link = f"{self.settings.PUBLIC_BASE_URL}{self.settings.EMAIL_VERIFY_PATH}?token={token}"
        text_body = f"Verify your email: {link}"
        html_body = f"<p>Verify your email:</p><p><a href='{link}'>Verify Email</a></p>"
        await self.send_email(to_email, "Verify your email", text_body, html_body)

    async def send_password_reset_email(self, to_email: str, token: str) -> None:
        link = f"{self.settings.PUBLIC_BASE_URL}{self.settings.PASSWORD_RESET_PATH}?token={token}"
        text_body = f"Reset your password: {link}"
        html_body = f"<p>Reset your password:</p><p><a href='{link}'>Reset Password</a></p>"
        await self.send_email(to_email, "Password reset", text_body, html_body)

    async def send_email_change_email(self, to_email: str, token: str) -> None:
        link = f"{self.settings.PUBLIC_BASE_URL}{self.settings.EMAIL_CHANGE_PATH}?token={token}"
        text_body = f"Confirm your email change: {link}"
        html_body = f"<p>Confirm your email change:</p><p><a href='{link}'>Confirm Email</a></p>"
        await self.send_email(to_email, "Confirm email change", text_body, html_body)

    async def send_invitation_email(self, to_email: str, org_name: str, token: str) -> None:
        link = f"{self.settings.PUBLIC_BASE_URL}/accept-invite?token={token}"
        text_body = f"You were invited to {org_name}. Accept: {link}"
        html_body = f"<p>You were invited to {org_name}.</p><p><a href='{link}'>Accept Invite</a></p>"
        await self.send_email(to_email, f"Invitation to {org_name}", text_body, html_body)
