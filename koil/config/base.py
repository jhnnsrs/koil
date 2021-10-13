from typing import Any, Dict
from pydantic.main import BaseModel
from pydantic import BaseSettings, env_settings
import yaml
import os




class ConfigError(Exception):
    pass


def yaml_config_settings_source(settings: BaseSettings) -> Dict[str, Any]:
    """
    A simple settings source that loads variables from a YAML file
    at the project's root.

    """
    file_path = settings.__config__.yaml_file
    group = settings.__config__.yaml_group
    try:
        with open(file_path,"r") as file:
            config = yaml.load(file, Loader=yaml.FullLoader)

        for subgroup in group.split("."):
            config = config[subgroup]
    except FileNotFoundError:
        print(f"No config File Found at {os.getcwd()}{file_path}")
        return {}


    return config




class BaseConfig(BaseSettings):
    """Container for the Configuration options
    parsed either from Env or from the yaml file

    Args:
        BaseModel ([type]): [description]

    Returns:
        [type]: [description]
    """

    class Config:
        extra = "ignore"

        @classmethod
        def customise_sources(
            cls,
            init_settings,
            env_settings,
            file_secret_settings,
        ):
            return (
                init_settings,
                env_settings,
                yaml_config_settings_source,
                file_secret_settings,
            )



    @classmethod
    def from_file(cls, file_path=None, **overrides):
        assert hasattr(cls.__config__,"yaml_group"), "Please specifiy your parent group in your Config Class to access it from a file "
        cls.__config__.yaml_file = file_path
        return cls(**overrides)
        
