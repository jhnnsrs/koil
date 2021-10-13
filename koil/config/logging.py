from herre.config.base import BaseConfig
from enum import Enum
from typing import List, Optional


class GrantType(str, Enum):
    IMPLICIT = "IMPLICIT"
    PASSWORD = "PASSWORD"
    CLIENT_CREDENTIALS = "CLIENT_CREDENTIALS"
    AUHORIZATION_CODE = "AUTHORIZATION_CODE"

class LoggingConfig(BaseConfig):

    stream: bool = False
    file: bool = False
    file_path: str = "logs.txt"
    level: str = "WARNING"

    class Config:
        yaml_group = "logging"
        env_prefix = "logging_"

    


    