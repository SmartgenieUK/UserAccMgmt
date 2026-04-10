from __future__ import annotations

import secrets
import string
from email.message import EmailMessage

import aiosmtplib

from app.core.config import Settings


class EmailService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def generate_otp(self) -> str:
        digits = string.digits
        return "".join(secrets.choice(digits) for _ in range(self.settings.OTP_LENGTH))

    async def send_email(self, to_email: str, subject: str, text_body: str, html_body: str | None = None) -> None:
        self._validate_recipient_domain(to_email)
        if self.settings.EMAIL_PROVIDER == "acs":
            await self._send_via_acs(to_email, subject, text_body, html_body)
        else:
            await self._send_via_smtp(to_email, subject, text_body, html_body)

    def _validate_recipient_domain(self, to_email: str) -> None:
        """Block email sends to domains not in the allowlist (when configured)."""
        if not self.settings.ALLOWED_EMAIL_DOMAINS:
            return
        domain = to_email.rsplit("@", 1)[-1].lower()
        allowed = [d.lower() for d in self.settings.ALLOWED_EMAIL_DOMAINS]
        if domain not in allowed:
            import logging
            logging.getLogger(__name__).warning(
                "Blocked email send to disallowed domain: %s (recipient: %s)", domain, to_email
            )
            raise ValueError(f"Email domain '{domain}' is not permitted")

    async def _send_via_acs(self, to_email: str, subject: str, text_body: str, html_body: str | None = None) -> None:
        from azure.communication.email import EmailClient

        client = EmailClient.from_connection_string(self.settings.ACS_CONNECTION_STRING)
        message = {
            "senderAddress": self.settings.EMAIL_FROM,
            "recipients": {"to": [{"address": to_email}]},
            "content": {"subject": subject, "plainText": text_body},
        }
        if html_body:
            message["content"]["html"] = html_body
        poller = client.begin_send(message)
        poller.result()

    async def _send_via_smtp(self, to_email: str, subject: str, text_body: str, html_body: str | None = None) -> None:
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
        otp = token  # token is now the OTP code
        text_body = f"Your verification code is: {otp}\n\nThis code expires in {self.settings.OTP_EXPIRE_MINUTES} minutes."
        html_body = f"""
        <div style="font-family: Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 24px;">
            <h2 style="color: #1e293b;">Verify your email</h2>
            <p style="color: #64748b;">Enter this code to verify your email address:</p>
            <div style="background: #f1f5f9; border-radius: 8px; padding: 20px; text-align: center; margin: 20px 0;">
                <span style="font-size: 32px; font-weight: 700; letter-spacing: 8px; color: #0f172a;">{otp}</span>
            </div>
            <p style="color: #94a3b8; font-size: 13px;">This code expires in {self.settings.OTP_EXPIRE_MINUTES} minutes. If you didn't request this, ignore this email.</p>
        </div>
        """
        await self.send_email(to_email, f"Your verification code: {otp}", text_body, html_body)

    async def send_password_reset_email(self, to_email: str, token: str) -> None:
        otp = token
        text_body = f"Your password reset code is: {otp}\n\nThis code expires in {self.settings.OTP_EXPIRE_MINUTES} minutes."
        html_body = f"""
        <div style="font-family: Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 24px;">
            <h2 style="color: #1e293b;">Reset your password</h2>
            <p style="color: #64748b;">Enter this code to reset your password:</p>
            <div style="background: #f1f5f9; border-radius: 8px; padding: 20px; text-align: center; margin: 20px 0;">
                <span style="font-size: 32px; font-weight: 700; letter-spacing: 8px; color: #0f172a;">{otp}</span>
            </div>
            <p style="color: #94a3b8; font-size: 13px;">This code expires in {self.settings.OTP_EXPIRE_MINUTES} minutes. If you didn't request this, ignore this email.</p>
        </div>
        """
        await self.send_email(to_email, f"Your password reset code: {otp}", text_body, html_body)

    async def send_email_change_email(self, to_email: str, token: str) -> None:
        otp = token
        text_body = f"Your email change confirmation code is: {otp}\n\nThis code expires in {self.settings.OTP_EXPIRE_MINUTES} minutes."
        html_body = f"""
        <div style="font-family: Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 24px;">
            <h2 style="color: #1e293b;">Confirm email change</h2>
            <p style="color: #64748b;">Enter this code to confirm your email change:</p>
            <div style="background: #f1f5f9; border-radius: 8px; padding: 20px; text-align: center; margin: 20px 0;">
                <span style="font-size: 32px; font-weight: 700; letter-spacing: 8px; color: #0f172a;">{otp}</span>
            </div>
            <p style="color: #94a3b8; font-size: 13px;">This code expires in {self.settings.OTP_EXPIRE_MINUTES} minutes. If you didn't request this, ignore this email.</p>
        </div>
        """
        await self.send_email(to_email, f"Your email change code: {otp}", text_body, html_body)

    async def send_invitation_email(self, to_email: str, org_name: str, token: str) -> None:
        link = f"{self.settings.PUBLIC_BASE_URL}/accept-invite?token={token}"
        text_body = f"You were invited to {org_name}. Accept: {link}"
        html_body = f"<p>You were invited to {org_name}.</p><p><a href='{link}'>Accept Invite</a></p>"
        await self.send_email(to_email, f"Invitation to {org_name}", text_body, html_body)
