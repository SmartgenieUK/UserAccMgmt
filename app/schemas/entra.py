from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# Inbound shape of a Microsoft Entra token-issuance-start callout. We read only what we need from
# data.authenticationContext and ignore the rest of Microsoft's envelope. (Microsoft Learn,
# custom-claims-provider-reference.)


class EntraUser(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    mail: str | None = None
    displayName: str | None = None
    userPrincipalName: str | None = None


class EntraServicePrincipal(BaseModel):
    model_config = ConfigDict(extra="ignore")
    appId: str | None = None


class EntraAuthContext(BaseModel):
    model_config = ConfigDict(extra="ignore")
    clientServicePrincipal: EntraServicePrincipal = EntraServicePrincipal()
    user: EntraUser


class EntraCalloutData(BaseModel):
    model_config = ConfigDict(extra="ignore")
    authenticationContext: EntraAuthContext


class EntraCalloutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data: EntraCalloutData


def provide_claims_response(claims: dict) -> dict:
    """The onTokenIssuanceStartResponseData envelope Entra expects, with one provideClaimsForToken action."""
    return {
        "data": {
            "@odata.type": "microsoft.graph.onTokenIssuanceStartResponseData",
            "actions": [
                {
                    "@odata.type": "microsoft.graph.tokenIssuanceStart.provideClaimsForToken",
                    "claims": claims,
                }
            ],
        }
    }
