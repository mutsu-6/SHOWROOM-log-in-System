import pytest

from showroom_keeper.config import Config, normalize_room_key


@pytest.mark.parametrize(
    "value,expected",
    [
        ("48_Yui_Oguri", "48_Yui_Oguri"),
        ("https://www.showroom-live.com/r/48_Yui_Oguri", "48_Yui_Oguri"),
        ("https://www.showroom-live.com/r/abc-123/?t=1", "abc-123"),
        ("https://www.showroom-live.com/room_key", "room_key"),
    ],
)
def test_normalize_room_key(value, expected):
    assert normalize_room_key(value) == expected


def test_normalize_room_key_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_room_key("not a room!")


def test_load_yaml_and_env(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "rooms: [a, 'https://www.showroom-live.com/r/b', a]\n"
        "credentials: {account_id: file_user, password: file_pw}\n"
        "status_poll_interval: 30\n",
        encoding="utf-8",
    )
    cfg = Config.load(p, env={"SHOWROOM_PASSWORD": "env_pw", "SHOWROOM_HEADLESS": "false"})
    assert cfg.rooms == ["a", "b"]
    assert cfg.credentials.account_id == "file_user"
    assert cfg.credentials.password == "env_pw"
    assert cfg.status_poll_interval == 30
    assert cfg.headless is False


def test_env_rooms_override(tmp_path):
    cfg = Config.load(None, env={"SHOWROOM_ROOMS": "x, y"})
    assert cfg.rooms == ["x", "y"]
    assert not cfg.credentials.available


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("typo_key: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        Config.load(p, env={})


def test_missing_explicit_config_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "nope.yaml", env={})


def test_proxy_and_channel_from_env():
    cfg = Config.load(None, env={"HTTPS_PROXY": "http://p:1", "SHOWROOM_BROWSER_CHANNEL": ""})
    assert cfg.proxy == "http://p:1"
    assert cfg.browser_channel == ""
    assert Config.load(None, env={}).browser_channel == "chrome"
