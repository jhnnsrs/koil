from herre.config.base import BaseConfig
from pydantic import Field
from enum import Enum
from typing import List, Optional


class GrantType(str, Enum):
    IMPLICIT = "IMPLICIT"
    PASSWORD = "PASSWORD"
    CLIENT_CREDENTIALS = "CLIENT_CREDENTIALS"
    AUTHORIZATION_CODE = "AUTHORIZATION_CODE"
    AUTHORIZATION_CODE_SERVER = "AUTHORIZATION_CODE_SERVER"

class HerreConfig(BaseConfig):

    secure: bool 
    host: str
    port: int
    client_id: str 
    client_secret: str
    authorization_grant_type: GrantType
    scopes: List[str]
    redirect_uri: Optional[str]
    jupyter_sync: bool = False

    class Config:
        yaml_group = "herre"
        env_prefix = "herre_"


    def __str__(self) -> str:
        return f"{'Secure' if self.secure else 'Insecure'} Connection to {self.host}:{self.port} on Grant {self.authorization_grant_type}"

    