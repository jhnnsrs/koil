from herre.auth import HerreClient


async def test_client_credentials():

    client = HerreClient(config_path="tests/configs/bergen.yaml")
    await client.login()
    assert client.headers is not None

