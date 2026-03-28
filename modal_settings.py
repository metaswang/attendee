import os
from dataclasses import dataclass


@dataclass(slots=True)
class ModalSettings:
    """
    Modal-related settings.

    Reads nested env vars like MODAL__TOKEN_ID and MODAL__TOKEN_SECRET,
    then exports standard env vars so the Modal SDK can authenticate.
    """

    token_id: str | None = None
    token_secret: str | None = None

    @classmethod
    def from_env(cls) -> "ModalSettings":
        return cls(
            token_id=os.getenv("MODAL__TOKEN_ID"),
            token_secret=os.getenv("MODAL__TOKEN_SECRET"),
        )

    def apply_sdk_auth_env(self) -> None:
        if self.token_id:
            os.environ["MODAL_TOKEN_ID"] = self.token_id
        if self.token_secret:
            os.environ["MODAL_TOKEN_SECRET"] = self.token_secret

