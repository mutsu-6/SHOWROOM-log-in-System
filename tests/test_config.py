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


def test_load_dotenv(tmp_path):
    from showroom_keeper.config import load_dotenv

    p = tmp_path / ".env"
    p.write_text('# c\nSHOWROOM_ACCOUNT_ID=me\nSHOWROOM_PASSWORD="p=w"\nSHOWROOM_ROOMS=\nEXISTING=new\n', encoding="utf-8")
    env = {"EXISTING": "old"}
    load_dotenv(p, env)
    assert env == {"EXISTING": "old", "SHOWROOM_ACCOUNT_ID": "me", "SHOWROOM_PASSWORD": "p=w"}


def test_record_targets_have_no_count_limit():
    from showroom_keeper.config import normalize_event_key

    assert normalize_event_key("https://www.showroom-live.com/event/healyourselfweek_9?block_id=1") == "healyourselfweek_9"
    cfg = Config.load(None, env={"SHOWROOM_RECORD_ROOMS": "a,https://www.showroom-live.com/r/b",
                                 "SHOWROOM_EVENTS": "https://www.showroom-live.com/event/ev1"})
    assert cfg.record_rooms == ["a", "b"] and cfg.events == ["ev1"]
    many = Config.load(None, env={
        "SHOWROOM_RECORD_ROOMS": ",".join(f"r{i}" for i in range(40)),
        "SHOWROOM_EVENTS": ",".join(f"e{i}" for i in range(5)),
    })
    assert len(many.record_rooms) == 40 and len(many.events) == 5
    assert many.keep_open_when_offline is True
